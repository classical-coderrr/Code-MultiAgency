"""Safe Workspace materialization for workflow outputs."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from ..repositories.sqlite import SQLiteRepository
from .artifact_paths import normalize_artifact_path
from ..workflow.models import utc_now
from ..code_company.security import SecretScanner
from ..code_company.artifact_contracts import path_allowed


class ArtifactService:
    """Persist run outputs without allowing paths to escape the run workspace."""

    _safe_name = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

    def __init__(self, repository: SQLiteRepository, workspace_root: str | Path) -> None:
        self.repository = repository
        self.workspace_root = Path(workspace_root).resolve()
        self.workspace_root.mkdir(parents=True, exist_ok=True)

    def materialize(
        self,
        run_id: str,
        workflow_id: str,
        context: dict[str, Any],
        final_report: str | None,
    ) -> list[dict[str, Any]]:
        """Write a Markdown report and any complete HTML returned by an Agent."""
        run_dir = self._run_dir(run_id)
        report = str(final_report or "").strip()
        source_artifacts = self._extract_structured_artifacts(context) if context.get("delivery_contract") else [
            *self._extract_structured_artifacts(context),
            *self._extract_source_artifacts(context),
        ]
        written_names: set[str] = set()
        written_owners: dict[str, str | None] = {}
        written_content: dict[str, str] = {}
        ownership = ((context.get("compiled_contract") or {}).get("artifact_ownership") or {}) if isinstance(context, dict) else {}
        verified_sources: list[tuple[str, str, str, str | None]] = []
        for name, mime_type, content, owner_step in source_artifacts:
            if name in written_names:
                if written_owners.get(name) != owner_step:
                    raise ValueError(f"Artifact {name} 被多个 Agent 重复登记")
                if written_content.get(name) != content:
                    raise ValueError(f"Artifact {name} 同一 Agent 登记了相互冲突的内容")
                continue
            if owner_step in ownership and not path_allowed(str(owner_step), name, ownership):
                raise ValueError(f"Artifact Ownership 拒绝 {owner_step} 写入 {name}")
            verified_sources.append((name, mime_type, content, owner_step))
            written_names.add(name)
            written_owners[name] = owner_step
            written_content[name] = content

        records: list[dict[str, Any]] = []
        if report:
            records.append(self._write(run_id, "final-report.md", "text/markdown; charset=utf-8", report))
        if context.get("delivery_contract") and context.get("delivery_gate"):
            metadata = {"schema_version": "1.0", "run_id": run_id, "workflow_id": workflow_id,
                        "contract": context["delivery_contract"], "contractHash": context.get("delivery_contract_hash"),
                        "delivery_gate": context["delivery_gate"], "validation": context.get("artifact_validation")}
            records.append(self._write(run_id, "delivery-report.json", "application/json; charset=utf-8", json.dumps(metadata, ensure_ascii=False, indent=2)))
        for name, mime_type, content, owner_step in verified_sources:
            records.append(self._write(run_id, name, mime_type, content, owner_step))

        html = self._extract_html(context)
        if html and "index.html" not in written_names and not context.get("delivery_contract"):
            records.append(self._write(run_id, "index.html", "text/html; charset=utf-8", html))

        # Keep the workspace directory creation explicit even when an Agent
        # only returned a report; later Tool/RAG nodes can reuse this location.
        run_dir.mkdir(parents=True, exist_ok=True)
        return [self._public_record(record, workflow_id) for record in records]

    def list_public(self, run_id: str) -> list[dict[str, Any]]:
        public: list[dict[str, Any]] = []
        for record in self.repository.list_artifacts(run_id):
            # Do not advertise stale database rows whose file was removed or
            # moved. They otherwise appear as downloadable artifacts that
            # always return 404.
            if self.resolve_path(run_id, str(record.get("id") or "")):
                public.append(self._public_record(record, ""))
        return public

    def create_archive(self, run_id: str, *, strict: bool = False) -> Path:
        """Create one safe ZIP containing every registered artifact for a run."""
        records = self.repository.list_artifacts(run_id)
        if not records:
            raise FileNotFoundError("No artifacts available for this run")

        run_dir = self._run_dir(run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        if strict:
            scan_files: list[tuple[str, str]] = []
            for record in records:
                resolved = self.resolve_path(run_id, str(record.get("id") or ""))
                if not resolved:
                    continue
                path = resolved[0]
                try:
                    scan_files.append((str(record.get("name") or ""), path.read_text(encoding="utf-8")))
                except (OSError, UnicodeDecodeError):
                    scan_files.append((str(record.get("name") or ""), ""))
            security = SecretScanner().scan(scan_files)
            if not security.passed:
                summary = "; ".join(f"{item.path}: {item.message}" for item in security.findings[:8])
                raise ValueError(f"Artifact security scan failed: {summary}")
        archive_path = run_dir / "artifacts.zip"
        temp_fd, temp_name = tempfile.mkstemp(prefix=".artifacts-", suffix=".zip", dir=run_dir)
        os.close(temp_fd)

        artifact_owners = {
            str(record.get("owner_step") or "").strip()
            for record in records
            if str(record.get("owner_step") or "").strip()
        }
        use_owner_directories = len(artifact_owners) > 1
        entry_paths: dict[str, Path] = {}
        registered_paths: set[Path] = set()
        for record in records:
            name = str(record.get("name") or "").strip()
            artifact_id = str(record.get("id") or "")
            if not normalize_artifact_path(name):
                continue
            resolved = self.resolve_path(run_id, artifact_id)
            if resolved:
                path = resolved[0]
                archive_name = self._archive_entry_name(record, use_owner_directories, artifact_owners)
                if archive_name:
                    entry_paths[archive_name] = path
                    registered_paths.add(path.resolve())

        # Include files physically present in the isolated Run workspace even
        # when a legacy/partial run did not persist its artifact row. Never
        # include the archive itself or an in-progress temporary archive.
        for path in (() if strict else run_dir.rglob("*")):
            if not path.is_file() or path == archive_path or path.name.startswith("."):
                continue
            if path.resolve() in registered_paths:
                continue
            # Unregistered ZIPs in a Run directory are usually an older or
            # manually downloaded archive. Including them would recursively
            # package stale deliverables into the new archive. A ZIP that is
            # a real Artifact is already included through ``records`` above.
            if path.suffix.lower() == ".zip":
                continue
            relative = path.relative_to(run_dir).as_posix()
            if normalize_artifact_path(relative):
                entry_paths.setdefault(relative, path.resolve())

        written = 0
        try:
            with zipfile.ZipFile(temp_name, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for name, path in sorted(entry_paths.items()):
                    archive.write(path, arcname=name)
                    written += 1
            if written == 0:
                raise FileNotFoundError("No readable artifacts available for this run")
            os.replace(temp_name, archive_path)
            return archive_path
        except Exception:
            try:
                Path(temp_name).unlink(missing_ok=True)
            except OSError:
                pass
            raise

    @staticmethod
    def _archive_entry_name(
        record: dict[str, Any],
        use_owner_directories: bool,
        artifact_owners: set[str] | None = None,
    ) -> str:
        """Keep multi-Agent deliverables in runnable owner project folders.

        Artifact files stay addressable by their original names in the API,
        while a ZIP containing both frontend and backend projects gets the
        conventional ``frontend/`` and ``backend/`` layout.  Already-prefixed
        paths are not prefixed a second time.
        """
        name = normalize_artifact_path(str(record.get("name") or ""))
        if not name:
            return ""
        owner = normalize_artifact_path(str(record.get("owner_step") or ""))
        if not use_owner_directories or not owner:
            return name
        owner_root = owner.split("/", 1)[0]
        owner_roots = {
            value.split("/", 1)[0]
            for value in (artifact_owners or set())
            if normalize_artifact_path(value)
        }
        # A Spring Boot + HTML delivery is one runnable project even though
        # Database and Frontend are produced by separate Agents. During Run
        # validation their ``src/main/resources`` files already share the
        # backend project root; preserve that exact layout in the ZIP.
        if (
            "backend" in owner_roots
            and owner_root in {"frontend", "database"}
            and name.startswith("src/main/resources/")
        ):
            return normalize_artifact_path(f"backend/{name}") or ""
        if name == owner_root or name.startswith(f"{owner_root}/"):
            return name
        return normalize_artifact_path(f"{owner_root}/{name}") or ""

    def resolve_path(self, run_id: str, artifact_id: str) -> tuple[Path, str] | None:
        record = self.repository.get_artifact(run_id, artifact_id)
        if not record:
            return None
        path = (self.workspace_root / record["relative_path"]).resolve()
        if not self._within_workspace(path) or not path.is_file():
            return None
        expected_sha = str(record.get("sha256") or "").strip().lower()
        if expected_sha:
            actual_sha = hashlib.sha256(path.read_bytes()).hexdigest().lower()
            if actual_sha != expected_sha:
                return None
        return path, str(record["mime_type"])

    def _run_dir(self, run_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", run_id):
            raise ValueError("Invalid run id for artifact workspace")
        path = (self.workspace_root / run_id).resolve()
        if not self._within_workspace(path):
            raise ValueError("Artifact workspace escapes configured root")
        return path

    def _write(self, run_id: str, name: str, mime_type: str, content: str, owner_step: str | None = None) -> dict[str, Any]:
        normalized_name = normalize_artifact_path(name)
        if not normalized_name:
            raise ValueError("Invalid artifact name")
        name = normalized_name
        run_dir = self._run_dir(run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        path = (run_dir / name).resolve()
        if not self._within_workspace(path):
            raise ValueError("Artifact path escapes run workspace")
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write beside the target and atomically replace it. A process crash
        # cannot leave a half-written source file that looks downloadable.
        temp_fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(temp_fd, "w", encoding="utf-8", newline="") as temp_file:
                temp_file.write(content)
                temp_file.flush()
                os.fsync(temp_file.fileno())
            os.replace(temp_name, path)
        except Exception:
            try:
                Path(temp_name).unlink(missing_ok=True)
            except OSError:
                pass
            raise
        relative_path = path.relative_to(self.workspace_root).as_posix()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        artifact_id = f"{run_id}_{name.replace('.', '_').replace('/', '_')}"
        record = {
            "id": artifact_id,
            "run_id": run_id,
            "name": name,
            "mime_type": mime_type,
            "relative_path": relative_path,
            "size_bytes": path.stat().st_size,
            "created_at": utc_now(),
            "sha256": digest,
            "owner_step": owner_step,
        }
        self.repository.upsert_artifact(
            artifact_id,
            run_id,
            name,
            mime_type,
            relative_path,
            record["size_bytes"],
            record["created_at"],
            record["sha256"],
            record["owner_step"],
        )
        # A materialized file is the stable candidate exposed to the user.
        # Repair code may create its own Candidate/Rejected records without
        # changing the public run_artifacts row until validation passes.
        self.repository.record_artifact_version(
            run_id=run_id,
            name=name,
            status="Stable",
            owner_step=owner_step,
            sha256=record["sha256"],
            metadata={"artifact_id": artifact_id, "materialized": True},
        )
        return record

    def _public_record(self, record: dict[str, Any], workflow_id: str) -> dict[str, Any]:
        run_id = str(record.get("run_id") or "")
        return {
            "id": record["id"],
            "name": record["name"],
            "mimeType": record["mime_type"],
            "sizeBytes": int(record.get("size_bytes") or 0),
            "createdAt": record.get("created_at"),
            "sha256": record.get("sha256"),
            "ownerStep": record.get("owner_step"),
            "integrityVerified": bool(record.get("sha256")),
            "workflowId": workflow_id,
            "previewable": record["mime_type"].startswith("text/html"),
        }

    @classmethod
    def _extract_structured_artifacts(cls, context: dict[str, Any]) -> list[tuple[str, str, str, str | None]]:
        """Read files produced by artifact-first generation from Graph State."""
        raw_files = context.get("__artifact_files__")
        if not isinstance(raw_files, list):
            return []
        language_mime = {
            "html": "text/html; charset=utf-8",
            "css": "text/css; charset=utf-8",
            "javascript": "text/javascript; charset=utf-8",
            "typescript": "text/plain; charset=utf-8",
            "tsx": "text/plain; charset=utf-8",
            "vue": "text/plain; charset=utf-8",
            "python": "text/x-python; charset=utf-8",
            "json": "application/json; charset=utf-8",
            "yaml": "text/yaml; charset=utf-8",
            "sql": "application/sql; charset=utf-8",
            "java": "text/x-java; charset=utf-8",
            "kotlin": "text/x-kotlin; charset=utf-8",
            "properties": "text/plain; charset=utf-8",
            "text": "text/plain; charset=utf-8",
            "markdown": "text/markdown; charset=utf-8",
            "svg": "image/svg+xml; charset=utf-8",
            "xml": "application/xml; charset=utf-8",
        }
        result: list[tuple[str, str, str, str | None]] = []
        for item in raw_files:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            content = item.get("content")
            if not name or not isinstance(content, str) or not content.strip():
                continue
            name = normalize_artifact_path(name)
            if not name:
                continue
            language = str(item.get("language") or "text").strip().lower()
            mime_type = language_mime.get(language, "text/plain; charset=utf-8")
            owner_step = str(item.get("step_id") or item.get("owner_step") or "").strip() or None
            result.append((name, mime_type, content, owner_step))
        return result

    @classmethod
    def _extract_source_artifacts(cls, context: dict[str, Any]) -> list[tuple[str, str, str, str | None]]:
        """Extract explicitly fenced source files from implementation outputs.

        Agents can return plans without creating files. When they do return
        fenced source, this keeps the source as a first-class Artifact instead
        of burying it in Markdown. Only implementation outputs are inspected;
        architecture and review prose are never treated as source files.
        """
        fence = re.compile(r"```(?P<info>[^\r\n`]*)\r?\n(?P<body>.*?)```", re.IGNORECASE | re.DOTALL)
        language_map = {
            "html": ("index.html", "text/html; charset=utf-8"),
            "htm": ("index.html", "text/html; charset=utf-8"),
            "css": ("style.css", "text/css; charset=utf-8"),
            "js": ("script.js", "text/javascript; charset=utf-8"),
            "javascript": ("script.js", "text/javascript; charset=utf-8"),
            "ts": ("main.ts", "text/plain; charset=utf-8"),
            "tsx": ("App.tsx", "text/plain; charset=utf-8"),
            "vue": ("App.vue", "text/plain; charset=utf-8"),
            "py": ("main.py", "text/x-python; charset=utf-8"),
            "python": ("main.py", "text/x-python; charset=utf-8"),
            "json": ("data.json", "application/json; charset=utf-8"),
            "yaml": ("config.yaml", "text/yaml; charset=utf-8"),
            "yml": ("config.yaml", "text/yaml; charset=utf-8"),
            "sql": ("schema.sql", "application/sql; charset=utf-8"),
        }
        outputs = (("frontend_result", "frontend"), ("backend_result", "backend"))
        used_names: set[str] = set()
        artifacts: list[tuple[str, str, str, str | None]] = []
        for context_key, owner in outputs:
            value = context.get(context_key)
            if not isinstance(value, str):
                continue
            counters: dict[str, int] = {}
            for match in fence.finditer(value):
                info = match.group("info").strip()
                tokens = info.split()
                language = tokens[0].lower() if tokens else ""
                if language not in language_map:
                    continue
                default_name, mime_type = language_map[language]
                explicit_name = cls._fence_filename(tokens[1:])
                name = explicit_name or default_name
                if not explicit_name:
                    stem, suffix = Path(name).stem, Path(name).suffix
                    counters[name] = counters.get(name, 0) + 1
                    if counters[name] > 1:
                        name = f"{stem}-{counters[name]}{suffix}"
                if name in used_names:
                    stem, suffix = Path(name).stem, Path(name).suffix
                    name = f"{owner}-{stem}{suffix}"
                name = normalize_artifact_path(name)
                if not name:
                    continue
                content = match.group("body").strip()
                if not content:
                    continue
                artifacts.append((name, mime_type, content, owner))
                used_names.add(name)
        return artifacts

    @staticmethod
    def _fence_filename(tokens: list[str]) -> str:
        for token in tokens:
            candidate = token.strip().strip("`")
            if "=" in candidate:
                key, value = candidate.split("=", 1)
                if key.lower() not in {"file", "filename", "path"}:
                    continue
                candidate = value
            if Path(candidate).suffix.lower() in {".html", ".htm", ".css", ".js", ".ts", ".tsx", ".vue", ".py", ".json", ".yaml", ".yml", ".sql"}:
                normalized = normalize_artifact_path(candidate)
                if normalized:
                    return normalized
        return ""

    def _within_workspace(self, path: Path) -> bool:
        try:
            path.relative_to(self.workspace_root)
            return True
        except ValueError:
            return False

    @staticmethod
    def _extract_html(context: dict[str, Any]) -> str:
        """Extract the first complete HTML document from Agent outputs."""
        candidates: list[str] = []
        for key in ("index_html", "html", "frontend_result", "final_report"):
            value = context.get(key)
            if value:
                candidates.append(str(value))
        for value in context.values():
            if isinstance(value, str) and value not in candidates:
                candidates.append(value)

        fence = re.compile(r"```(?:html|htm)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)
        for candidate in candidates:
            fenced = [match.strip() for match in fence.findall(candidate) if match.strip()]
            for item in [*fenced, candidate.strip()]:
                start = re.search(r"<!doctype\s+html|<html(?:\s|>)", item, re.IGNORECASE)
                if not start:
                    continue
                html = item[start.start():]
                end = re.search(r"</html>\s*$", html, re.IGNORECASE)
                if end:
                    return html[: end.end()].strip()
        return ""
