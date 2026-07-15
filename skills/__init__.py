from __future__ import annotations

from pathlib import Path
from typing import Any


DEFAULT_DATA_ROOT = Path(__file__).resolve().parents[1] / "data"


class SkillError(Exception):
    # advanced4: structured Skill exception with stable code/category fields for B2 and B3 error payloads.
    def __init__(
        self,
        message: str,
        code: str,
        category: str,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.category = category
        self.retryable = retryable
        self.details = details or {}


_ERROR_CLASSIFICATION = {  # advanced4: map common Python exceptions to stable B2 error codes.
    FileNotFoundError: ("FILE_NOT_FOUND", "file", False),
    PermissionError: ("PERMISSION_DENIED", "file", False),
    TimeoutError: ("TIMEOUT", "runtime", True),
    OSError: ("IO_ERROR", "file", True),
    TypeError: ("TYPE_ERROR", "input", False),
    ValueError: ("VALUE_ERROR", "input", False),
}


def skill_error_payload(exc: Exception) -> dict:
    # advanced4: keep old type/message while adding code/category/retryable/details for finer error handling.
    if isinstance(exc, SkillError):
        return {
            "type": type(exc).__name__,
            "code": exc.code,
            "category": exc.category,
            "message": str(exc),
            "retryable": exc.retryable,
            "details": exc.details,
        }
    for exception_type, (code, category, retryable) in _ERROR_CLASSIFICATION.items():
        if isinstance(exc, exception_type):
            return {
                "type": type(exc).__name__,
                "code": code,
                "category": category,
                "message": str(exc),
                "retryable": retryable,
                "details": {},
            }
    return {
        "type": type(exc).__name__,
        "code": "UNKNOWN_ERROR",
        "category": "unknown",
        "message": str(exc),
        "retryable": False,
        "details": {},
    }


def resolve_data_path(path: str, data_root: str | None = None) -> tuple[Path, Path]:
    root = Path(data_root).resolve() if data_root else DEFAULT_DATA_ROOT.resolve()
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = candidate.resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise SkillError(  # advanced4: classify unsafe path traversal separately from generic value errors.
            f"path escapes data root: {path}",
            code="PATH_ESCAPE",
            category="security",
            retryable=False,
            details={"path": path, "data_root": str(root)},
        ) from exc
    return candidate, root
