"""Code Company runtime adapters built on the generic platform core."""

from importlib import import_module

_EXPORTS = {
    "CodeCompanyRuntime": ".runtime",
    "VerificationEngine": ".verification",
    "ContractCompiler": ".contract_compiler",
    "CodingAgentLoop": ".coding_loop",
    "CodingLoopConfig": ".coding_loop",
    "CodingLoopResult": ".coding_loop",
    "SecretScanner": ".security",
    "SecretScanResult": ".security",
    "RepairEngine": ".repair",
}

__all__ = list(_EXPORTS)


def __getattr__(name: str):
    """Avoid importing verification while artifact_validator is still loading."""
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name, __name__), name)
    globals()[name] = value
    return value
