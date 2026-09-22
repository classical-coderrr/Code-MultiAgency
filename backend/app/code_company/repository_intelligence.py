"""Small, deterministic repository intelligence for Coding Agents.

The index is intentionally local and bounded.  It gives an Agent a map of a
repository without copying the whole repository into an LLM prompt.  A later
Tree-sitter/LSP adapter can implement the same contract without changing the
orchestrator.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..platform.workspace import LocalWorkspaceService, WorkspaceRef


@dataclass(frozen=True, slots=True)
class RepositorySymbol:
    path: str
    kind: str
    name: str
    line: int


@dataclass(slots=True)
class RepositoryIndex:
    root: str
    tree: list[str] = field(default_factory=list)
    symbols: list[RepositorySymbol] = field(default_factory=list)
    imports: dict[str, list[str]] = field(default_factory=dict)
    dependencies: list[str] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "tree": list(self.tree),
            "symbols": [asdict(item) for item in self.symbols],
            "imports": self.imports,
            "dependencies": list(self.dependencies),
            "summary": self.summary,
        }

    def relevant_files(self, query: str, *, limit: int = 20) -> list[str]:
        terms = {term.lower() for term in re.findall(r"[\w.-]+", str(query or "")) if len(term) > 1}
        scored: list[tuple[int, str]] = []
        for path in self.tree:
            lower = path.lower()
            score = sum(4 for term in terms if term in lower)
            score += sum(2 for symbol in self.symbols if symbol.path == path and symbol.name.lower() in terms)
            if score:
                scored.append((score, path))
        return [path for _, path in sorted(scored, key=lambda item: (-item[0], item[1]))[: max(1, limit)]]


class RepositoryIndexer:
    """Build a bounded tree/symbol/import/dependency index for one Workspace."""

    _ignored_dirs = {".git", ".venv", "node_modules", "target", "dist", "build", "logs", "workspace", "checkpoints", "private-cache"}
    _source_suffixes = {".py", ".java", ".kt", ".js", ".ts", ".tsx", ".vue", ".html", ".css", ".sql", ".yml", ".yaml", ".json", ".xml", ".md"}
    _symbol_patterns = (
        ("class", re.compile(r"^\s*(?:public\s+|private\s+|protected\s+|export\s+|async\s+)*class\s+(\w+)")),
        ("function", re.compile(r"^\s*(?:export\s+|async\s+|def\s+|function\s+)?(?:def|function)\s+(\w+)\s*\(")),
        ("method", re.compile(r"^\s*(?:public|private|protected|static|async|override|def|fun)\s+[\w<>\[\], ?]+\s+(\w+)\s*\(")),
    )

    def __init__(self, workspace_service: LocalWorkspaceService, *, max_files: int = 800, max_file_bytes: int = 512 * 1024) -> None:
        self.workspace_service = workspace_service
        self.max_files = max(1, int(max_files))
        self.max_file_bytes = max(1024, int(max_file_bytes))

    def index(self, workspace: WorkspaceRef | dict[str, Any]) -> RepositoryIndex:
        root = Path(str(workspace.worktree_path if isinstance(workspace, WorkspaceRef) else workspace.get("worktree_path", ""))).resolve()
        if not root.is_dir():
            return RepositoryIndex(str(root), summary={"fileCount": 0, "truncated": False})
        tree: list[str] = []
        symbols: list[RepositorySymbol] = []
        imports: dict[str, list[str]] = {}
        dependencies: set[str] = set()
        by_suffix: dict[str, int] = {}
        truncated = False
        for path in sorted(root.rglob("*")):
            if len(tree) >= self.max_files:
                truncated = True
                break
            if not path.is_file() or any(part.lower() in self._ignored_dirs for part in path.relative_to(root).parts):
                continue
            relative = path.relative_to(root).as_posix()
            if not self.workspace_service.can_read(workspace, relative):
                continue
            tree.append(relative)
            suffix = path.suffix.lower()
            by_suffix[suffix or "<none>"] = by_suffix.get(suffix or "<none>", 0) + 1
            if suffix not in self._source_suffixes or path.stat().st_size > self.max_file_bytes:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            file_imports = self._imports(text)
            if file_imports:
                imports[relative] = file_imports
                dependencies.update(file_imports)
            for kind, pattern in self._symbol_patterns:
                for line_no, line in enumerate(text.splitlines(), 1):
                    match = pattern.search(line)
                    if match:
                        symbols.append(RepositorySymbol(relative, kind, match.group(1), line_no))
        return RepositoryIndex(
            root=str(root),
            tree=tree,
            symbols=symbols[:4000],
            imports=imports,
            dependencies=sorted(dependencies)[:1000],
            summary={"fileCount": len(tree), "symbolCount": len(symbols), "dependencyCount": len(dependencies), "extensions": by_suffix, "truncated": truncated},
        )

    @staticmethod
    def _imports(text: str) -> list[str]:
        values: list[str] = []
        patterns = (
            r"^\s*import\s+([\w.]+)",
            r"^\s*from\s+([\w.]+)\s+import",
            r"^\s*#include\s*[<\"]([^>\"]+)",
            r"^\s*import\s+['\"]([^'\"]+)['\"]",
            r"^\s*require\(['\"]([^'\"]+)['\"]\)",
        )
        for pattern in patterns:
            values.extend(match.group(1) for match in re.finditer(pattern, text, re.MULTILINE))
        return list(dict.fromkeys(values))[:100]
