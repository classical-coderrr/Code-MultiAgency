"""Pre-generation ownership and file-dependency enforcement."""

from __future__ import annotations

import fnmatch
from pathlib import PurePath
from dataclasses import replace
from typing import Any, Callable, Iterable, TypeVar


T = TypeVar("T")
_LANGUAGES = {".java": "java", ".vue": "vue", ".js": "javascript", ".ts": "typescript", ".css": "css", ".html": "html", ".sql": "sql", ".xml": "xml", ".json": "json", ".yml": "yaml"}


def path_allowed(owner: str, path: str, ownership: dict[str, Any]) -> bool:
    normalized = str(path or "").replace("\\", "/").lstrip("/")
    patterns = ownership.get(owner) or []
    if isinstance(patterns, str):
        patterns = [patterns]
    return bool(patterns) and any(fnmatch.fnmatch(normalized.lower(), str(pattern).replace("\\", "/").lower()) for pattern in patterns)


def enforce_artifact_plan(
    plan: Iterable[T],
    *,
    owner: str,
    compiled_contract: dict[str, Any] | None,
    missing_factory: Callable[[dict[str, Any]], T] | None = None,
) -> tuple[list[T], list[str]]:
    """Deny cross-owner files, merge required plan items and validate symbols."""
    compiled = compiled_contract or {}
    ownership = compiled.get("artifact_ownership") or {}
    if not ownership or owner not in ownership:
        return list(plan), []
    accepted: list[T] = []
    denied: list[str] = []
    by_name: dict[str, T] = {}
    for item in plan:
        name = str(getattr(item, "name", ""))
        if path_allowed(owner, name, ownership):
            accepted.append(item)
            by_name[name] = item
        else:
            denied.append(name)

    path_aliases: dict[str, str] = {}
    for row in compiled.get("file_plan") or []:
        if not isinstance(row, dict) or str(row.get("owner")) != owner:
            continue
        path = str(row.get("path") or "")
        if not path:
            continue
        provides = tuple(str(value) for value in row.get("provides") or [])
        requires = tuple(str(value) for value in row.get("requires") or [])
        dependencies = tuple(str(value) for value in row.get("depends_on_files") or [])
        current = by_name.get(path)
        if current is None:
            same_name = [item for item in accepted if PurePath(str(getattr(item, "name", ""))).name == PurePath(path).name]
            current = same_name[0] if len(same_name) == 1 else None
        if current is None and path.endswith("/Application.java"):
            applications = [item for item in accepted if str(getattr(item, "name", "")).endswith("Application.java")]
            current = applications[0] if len(applications) == 1 else None
        if current is None and path == "src/main.js":
            current = by_name.get("src/main.ts")
        if current is not None:
            actual_path = str(getattr(current, "name", path))
            path_aliases[path] = actual_path
            updated = replace(
                current,
                owner=owner,
                provides=provides,
                requires=requires,
                depends_on_files=dependencies,
            )
            accepted[accepted.index(current)] = updated
            by_name[actual_path] = updated
            continue
        # Preserve the package root selected by the existing Java plan. A
        # second, hard-coded root would compile into a disconnected package.
        target_path = path
        if path.startswith("src/main/java/") and accepted:
            java_paths = [str(getattr(item, "name", "")) for item in accepted if str(getattr(item, "name", "")).endswith(".java")]
            if java_paths:
                package_root = str(PurePath(java_paths[0]).parent).replace("\\", "/")
                if package_root.rsplit("/", 1)[-1] in {"entity", "model", "repository", "service", "controller", "dto"}:
                    package_root = package_root.rsplit("/", 1)[0]
                target_path = f"{package_root}/{PurePath(path).name}"
        if not path_allowed(owner, target_path, ownership):
            raise ValueError(f"冻结文件计划越过 {owner} 的文件归属边界：{target_path}")
        template = next(iter(accepted), None)
        if template is not None:
            item = replace(
                template,
                name=target_path,
                language=_LANGUAGES.get(PurePath(target_path).suffix.lower(), "text"),
                purpose=f"冻结文件计划要求的 {', '.join(row.get('provides') or [path])}",
                estimated_tokens=1024,
                parts=(),
                owner=owner,
                provides=provides,
                requires=requires,
                depends_on_files=dependencies,
            )
        elif missing_factory is not None:
            item = missing_factory({**row, "path": target_path})
        else:
            raise ValueError(f"冻结文件计划缺少 {path}，且没有文件生成器")
        accepted.append(item)
        by_name[target_path] = item
        path_aliases[path] = target_path

    if path_aliases:
        for index, item in enumerate(accepted):
            dependencies = tuple(path_aliases.get(str(value), str(value)) for value in getattr(item, "depends_on_files", ()))
            if dependencies != getattr(item, "depends_on_files", ()):
                accepted[index] = replace(item, depends_on_files=dependencies)
        by_name = {str(getattr(item, "name", "")): item for item in accepted}

    providers: set[str] = set()
    if (compiled.get("openapi") or {}).get("paths"):
        providers.add("ApiContract")
    if str((compiled.get("database_schema") or {}).get("mode") or "none") != "none":
        providers.add("DatabaseSchema")
    if (compiled.get("dependency_manifest") or {}).get("backend"):
        providers.add("BackendDependencies")
    if (compiled.get("dependency_manifest") or {}).get("frontend"):
        providers.add("FrontendDependencies")
    for item in accepted:
        providers.update(str(value) for value in getattr(item, "provides", ()) if str(value))
    unresolved: list[str] = []
    for item in accepted:
        for required in getattr(item, "requires", ()):
            if str(required) not in providers:
                unresolved.append(f"{getattr(item, 'name', '')} requires {required}")
        for dependency in getattr(item, "depends_on_files", ()):
            if str(dependency) not in by_name:
                unresolved.append(f"{getattr(item, 'name', '')} depends_on_files {dependency}")
    if unresolved:
        raise ValueError("文件依赖计划不完整：" + "；".join(unresolved[:12]))
    # Stable topological order: independent files retain the planner's order.
    ordered: list[T] = []
    pending = list(accepted)
    done: set[str] = set()
    while pending:
        ready = [item for item in pending if all(str(dep) in done for dep in getattr(item, "depends_on_files", ()))]
        if not ready:
            raise ValueError("文件依赖计划存在环，无法生成：" + ", ".join(str(getattr(item, "name", "")) for item in pending[:8]))
        for item in ready:
            ordered.append(item)
            done.add(str(getattr(item, "name", "")))
            pending.remove(item)
    return ordered, denied


def owner_for_path(path: str, ownership: dict[str, Any]) -> str | None:
    matches = [owner for owner in ownership if path_allowed(owner, path, ownership)]
    return matches[0] if len(matches) == 1 else None
