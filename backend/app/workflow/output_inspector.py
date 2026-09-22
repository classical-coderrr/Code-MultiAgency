"""Provider response completeness and lightweight format validation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from html.parser import HTMLParser

from ..llm.base import LLMResponse


@dataclass(frozen=True, slots=True)
class OutputInspection:
    complete: bool
    partial: bool
    reason: str | None = None
    warnings: tuple[str, ...] = ()


class _BalanceParser(HTMLParser):
    _void_tags = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.stack: list[str] = []
        self.invalid = False

    def handle_starttag(self, tag: str, attrs) -> None:  # type: ignore[no-untyped-def]
        if tag.lower() not in self._void_tags:
            self.stack.append(tag.lower())

    def handle_startendtag(self, tag: str, attrs) -> None:  # type: ignore[no-untyped-def]
        return

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        if not self.stack or self.stack[-1] != normalized:
            self.invalid = True
            return
        self.stack.pop()


def _json_candidate(text: str) -> str:
    value = text.strip()
    if value.startswith("```"):
        lines = value.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        value = "\n".join(lines).strip()
    return value


def _validate_json(text: str) -> bool:
    try:
        json.loads(_json_candidate(text))
    except (TypeError, ValueError):
        return False
    return True


def _validate_html(text: str) -> bool:
    parser = _BalanceParser()
    try:
        parser.feed(text)
        parser.close()
    except (TypeError, ValueError):
        return False
    return not parser.invalid and not parser.stack


def inspect_response(response: LLMResponse, output_format: str = "text") -> OutputInspection:
    text = str(response.message_content if response.message_content is not None else response.text or "")
    if not text.strip():
        return OutputInspection(False, False, "empty_output")

    finish_reason = str(response.finish_reason or "").strip().lower()
    if finish_reason == "length":
        return OutputInspection(
            False,
            True,
            "output_truncated",
            ("Provider stopped at the output limit; the response may be incomplete.",),
        )

    normalized_format = str(output_format or "text").strip().lower()
    if normalized_format == "json" and not _validate_json(text):
        return OutputInspection(False, False, "invalid_json")
    if normalized_format == "html" and not _validate_html(text):
        return OutputInspection(False, False, "invalid_html")
    return OutputInspection(True, False)
