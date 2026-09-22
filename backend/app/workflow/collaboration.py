"""Bounded, auditable Agent consultation for cross-owner delivery failures.

Messages are proposals, not authority to change a frozen contract or to pass a
Gate.  The deterministic repair planner and validator retain those decisions.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ..llm.base import LLMResponse
from ..repositories.sqlite import SQLiteRepository


GenerateCallback = Callable[[str, str], Awaitable[LLMResponse]]
EmitCallback = Callable[[str, dict[str, Any]], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class ConsultationResult:
    conversation_id: str
    status: str
    guidance: dict[str, str]
    responses: tuple[LLMResponse, ...] = ()


class CollaborationCoordinator:
    """Two-round mailbox: owner proposals, then peer review.

    The mailbox is durable and idempotent, so resuming a Run does not repeat a
    completed consultation.  There is no unbounded Agent-to-Agent conversation.
    """

    def __init__(self, repository: SQLiteRepository) -> None:
        self.repository = repository

    async def consult(
        self,
        *,
        run_id: str,
        fingerprint: str,
        owners: list[str],
        evidence: list[dict[str, Any]],
        role_contracts: dict[str, Any],
        generate: GenerateCallback,
        emit: EmitCallback,
        max_rounds: int = 2,
        timeout_seconds: float = 40.0,
    ) -> ConsultationResult:
        participants = list(dict.fromkeys(str(owner) for owner in owners if str(owner)))[:3]
        conversation_id = "repair_" + hashlib.sha256(
            f"{run_id}:{fingerprint}:{','.join(sorted(participants))}".encode("utf-8")
        ).hexdigest()[:20]
        if len(participants) < 2:
            return ConsultationResult(conversation_id, "not_needed", {})

        cached = {
            (item["round_number"], item["sender"], item["recipient"], item["act"]): item
            for item in self.repository.list_collaboration_messages(run_id, conversation_id)
        }
        decision = cached.get((3, "coordinator", "repair", "decision"))
        if decision:
            payload = decision["payload"]
            return ConsultationResult(
                conversation_id,
                str(payload.get("status") or "incomplete"),
                {str(key): str(value) for key, value in (payload.get("guidance") or {}).items()},
            )

        responses: list[LLMResponse] = []
        proposals: dict[str, dict[str, Any]] = {}
        reviews: dict[str, dict[str, Any]] = {}
        failed_checks = [
            {
                "id": str(item.get("id") or "")[:120],
                "target": str(item.get("target") or "")[:80],
                "message": str(item.get("message") or "")[:900],
            }
            for item in evidence if str(item.get("status")) == "failed"
        ][:12]

        async def record(round_number: int, sender: str, recipient: str, act: str, payload: dict[str, Any]) -> None:
            key = (round_number, sender, recipient, act)
            if key in cached:
                return
            message_id = hashlib.sha256(
                f"{conversation_id}:{round_number}:{sender}:{recipient}:{act}".encode("utf-8")
            ).hexdigest()
            self.repository.append_collaboration_message(
                message_id=message_id, run_id=run_id, conversation_id=conversation_id,
                sender=sender, recipient=recipient, act=act,
                round_number=round_number, payload=payload,
            )
            cached[key] = {"payload": payload}
            await emit("collaboration.message", {
                "conversationId": conversation_id, "round": round_number,
                "sender": sender, "recipient": recipient, "act": act,
                "summary": str(payload.get("diagnosis") or payload.get("action") or payload.get("reason") or "")[:300],
            })

        async def ask(owner: str, round_number: int) -> None:
            response_act = "propose" if round_number == 1 else "review"
            stored = cached.get((round_number, owner, "coordinator", response_act))
            if stored:
                payload = stored["payload"]
                if round_number == 1:
                    proposals[owner] = payload
                else:
                    reviews[owner] = payload
                return
            peers = {key: value for key, value in proposals.items() if key != owner}
            raw_contract = role_contracts.get(owner)
            raw_contract = raw_contract if isinstance(raw_contract, dict) else {}
            # API/entity facts must survive truncation; cosmetic metadata comes last.
            scoped_contract = {
                key: raw_contract[key]
                for key in ("apis", "entities", "proxy_paths", "ownership", "file_plan", "database", "backend", "frontend", "constraints")
                if key in raw_contract
            }
            own_contract = json.dumps(scoped_contract, ensure_ascii=False, default=str)[:5000]
            prompt = (
                "你正在参加一次有上限的跨 Agent 故障协商，只提出建议，不写文件、不改冻结合同。"
                "只能建议修改自己拥有的成果物；最终是否通过由确定性 Gate 决定。"
                "仅返回 JSON 对象，不要 Markdown。\n"
                f"你的责任域：{owner}\n冻结角色合同：{own_contract}\n"
                f"失败证据：{json.dumps(failed_checks, ensure_ascii=False)[:5000]}\n"
            )
            if round_number == 1:
                prompt += (
                    "提出最小修复建议。字段：diagnosis（原因）、action（只涉及自己责任域的动作）、"
                    "needs_contract_change（布尔值）。每个文本字段不超过 300 字。"
                )
            else:
                prompt += (
                    f"其他 Agent 的提议：{json.dumps(peers, ensure_ascii=False)[:3000]}\n"
                    "检查与你的接口是否冲突。字段：agree（布尔值）、action（你自己的调整动作）、"
                    "reason（分歧原因或同意依据）、needs_contract_change（布尔值）。"
                    "不得为达成一致而改写冻结合同。"
                )
            await record(round_number, "coordinator", owner, "query", {"topic": "cross_owner_repair"})
            try:
                response = await generate(owner, prompt)
                responses.append(response)
                if str(response.finish_reason or "").lower() == "length":
                    raise ValueError("consultation output reached provider limit")
                payload = self._parse_response(response.text, round_number)
                await record(round_number, owner, "coordinator", response_act, payload)
                if round_number == 1:
                    proposals[owner] = payload
                else:
                    reviews[owner] = payload
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await record(round_number, owner, "coordinator", "error", {
                    "reason": f"{type(exc).__name__}: {str(exc)[:160]}"
                })

        async def discuss() -> None:
            await emit("collaboration.started", {
                "conversationId": conversation_id, "owners": participants,
                "rounds": max_rounds,
            })
            await asyncio.gather(*(ask(owner, 1) for owner in participants))
            if max_rounds >= 2 and len(proposals) == len(participants):
                await asyncio.gather(*(ask(owner, 2) for owner in participants))

        timed_out = False
        try:
            async with asyncio.timeout(max(1.0, min(float(timeout_seconds), 60.0))):
                await discuss()
        except TimeoutError:
            timed_out = True

        change_requested = any(
            bool(item.get("needs_contract_change"))
            for item in [*proposals.values(), *reviews.values()]
        )
        if timed_out or len(proposals) != len(participants):
            status = "incomplete"
        elif change_requested:
            status = "contract_change_requested"
        elif max_rounds >= 2 and len(reviews) == len(participants):
            status = "consensus" if all(item.get("agree") is True for item in reviews.values()) else "disputed"
        else:
            status = "advisory"
        guidance = {} if change_requested else {
            owner: "；".join(filter(None, [
                str(proposals.get(owner, {}).get("action") or ""),
                str(reviews.get(owner, {}).get("action") or ""),
            ]))[:600]
            for owner in participants if owner in proposals
        }
        await record(3, "coordinator", "repair", "decision", {
            "status": status, "guidance": guidance,
            "owners": participants, "contract_change_requested": change_requested,
            "reason": "协商仅提供建议；冻结合同和确定性验证保持权威。",
        })
        await emit("collaboration.completed", {
            "conversationId": conversation_id, "status": status,
            "owners": participants, "proposals": len(proposals), "reviews": len(reviews),
            "contractChangeRequested": change_requested,
        })
        return ConsultationResult(conversation_id, status, guidance, tuple(responses))

    @staticmethod
    def _parse_response(text: str, round_number: int) -> dict[str, Any]:
        raw = str(text or "").strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("consultation response must be a JSON object")
        if round_number == 1:
            if not isinstance(value.get("diagnosis"), str) or not value["diagnosis"].strip():
                raise ValueError("consultation proposal requires a diagnosis")
            if not isinstance(value.get("action"), str) or not value["action"].strip():
                raise ValueError("consultation proposal requires an action")
            return {
                "diagnosis": str(value.get("diagnosis") or "")[:600],
                "action": str(value.get("action") or "")[:600],
                "needs_contract_change": value.get("needs_contract_change") is True,
            }
        if not isinstance(value.get("agree"), bool):
            raise ValueError("consultation review requires a boolean agree field")
        return {
            "agree": value.get("agree") is True,
            "action": str(value.get("action") or "")[:600],
            "reason": str(value.get("reason") or "")[:600],
            "needs_contract_change": value.get("needs_contract_change") is True,
        }
