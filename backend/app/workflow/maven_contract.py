"""Small, deterministic corrections for known generated Maven mistakes."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET


def normalize_h2_flyway_dependency(content: str) -> tuple[str, bool]:
    """H2 support belongs to flyway-core, not flyway-database-h2.

    Preserve unrelated dependencies, comments and formatting. Malformed XML
    is left untouched so validation reports it, rather than guessing a POM.
    """
    try:
        ET.fromstring(content)
    except ET.ParseError:
        return content, False
    masked = re.sub(r"<!--.*?-->", lambda match: " " * len(match.group()), content, flags=re.DOTALL)
    blocks = list(re.finditer(r"<dependency\b[^>]*>.*?</dependency\s*>", masked, re.DOTALL))
    scopes = list(re.finditer(r"<dependencies\b[^>]*>.*?</dependencies\s*>", masked, re.DOTALL))
    def scope_key(block: re.Match[str]) -> int:
        return next((scope.start() for scope in scopes if scope.start() <= block.start() < scope.end()), -1)
    def coordinate(block: str, tag: str) -> str:
        match = re.search(rf"<{tag}>\s*([^<]+?)\s*</{tag}>", block)
        return match.group(1).strip() if match else ""
    core_scopes = {
        scope_key(block) for block in blocks
        if coordinate(block.group(), "groupId") == "org.flywaydb"
        and coordinate(block.group(), "artifactId") == "flyway-core"
    }
    changed = False
    for block in reversed(blocks):
        text = content[block.start():block.end()]
        if coordinate(text, "groupId") != "org.flywaydb" or coordinate(text, "artifactId") != "flyway-database-h2":
            continue
        key = scope_key(block)
        replacement = "" if key in core_scopes else re.sub(
            r"(<artifactId>\s*)flyway-database-h2(\s*</artifactId>)", r"\1flyway-core\2", text,
        )
        content = content[:block.start()] + replacement + content[block.end():]
        core_scopes.add(key)
        changed = True
    return content, changed
