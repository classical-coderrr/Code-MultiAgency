"""Secret and artifact allowlist checks for Code Company delivery."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Iterable


@dataclass(frozen=True, slots=True)
class SecretFinding:
    path: str
    rule: str
    message: str


@dataclass(frozen=True, slots=True)
class SecretScanResult:
    passed: bool
    findings: tuple[SecretFinding, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "findings": [{"path": item.path, "rule": item.rule, "message": item.message} for item in self.findings],
        }


class SecretScanner:
    denied_names = {".env", ".env.local", ".env.production", "id_rsa", "credentials.json"}
    denied_suffixes = {".db", ".db-wal", ".db-shm", ".pem", ".key", ".p12", ".pfx"}
    patterns = (
        ("provider-api-key", re.compile(r"(?i)(?:api[_-]?key|access[_-]?token)\s*[:=]\s*['\"]?sk-[A-Za-z0-9._-]{8,}")),
        ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
        ("bearer-token", re.compile(r"(?i)bearer\s+[A-Za-z0-9._-]{20,}")),
    )

    def scan(self, files: Iterable[tuple[str, str]]) -> SecretScanResult:
        findings: list[SecretFinding] = []
        for name, content in files:
            normalized = str(name).replace("\\", "/")
            path = PurePosixPath(normalized)
            basename = path.name.lower()
            if basename in self.denied_names or any(basename.endswith(item) for item in self.denied_suffixes) or ".git" in path.parts:
                findings.append(SecretFinding(normalized, "artifact-allowlist", "文件类型或路径禁止进入交付物"))
                continue
            for rule, pattern in self.patterns:
                if pattern.search(str(content or "")):
                    findings.append(SecretFinding(normalized, rule, "检测到疑似凭据，已阻止打包"))
        return SecretScanResult(not findings, tuple(findings))
