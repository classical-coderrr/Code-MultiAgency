"""Small, provider-neutral platform boundaries.

The platform package intentionally contains contracts and safe local adapters.
Company-specific orchestration belongs in ``app.code_company`` and the legacy
workflow package remains the compatibility layer during the migration.
"""

from .tool_gateway import LocalToolGateway, ToolCall, ToolPolicy, ToolResult
from .sandbox import LocalSandbox, SandboxPolicy, SandboxResult
from .workspace import LocalWorkspaceService, WorkspaceRef

__all__ = [
    "LocalToolGateway",
    "LocalSandbox",
    "LocalWorkspaceService",
    "ToolCall",
    "ToolPolicy",
    "ToolResult",
    "SandboxPolicy",
    "SandboxResult",
    "WorkspaceRef",
]
