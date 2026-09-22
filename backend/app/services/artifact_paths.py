"""Shared safety rules for relative Artifact paths.

Generated projects need normal source trees such as ``src/main.js`` and
``src/main/java/com/example/Application.java``.  Keeping the path policy in a
small dependency-free module lets the generator, validator and materializer
agree on those paths without allowing traversal outside a Run workspace.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath


_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def normalize_artifact_path(value: object) -> str | None:
    """Return a safe POSIX relative path, or ``None`` when it is unsafe."""

    raw = str(value or "").strip().replace("\\", "/")
    if not raw or raw.startswith("/") or ":" in raw.split("/", 1)[0]:
        return None
    path = PurePosixPath(raw)
    parts = path.parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        return None
    if not all(_SAFE_SEGMENT.fullmatch(part) for part in parts):
        return None
    return "/".join(parts)


def is_safe_artifact_path(value: object) -> bool:
    return normalize_artifact_path(value) is not None
