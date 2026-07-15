from __future__ import annotations

from pathlib import Path


STATE_FILE = Path(__file__).with_name(".flaky_retry_probe_state")


def flaky_retry_probe(text: str) -> dict:
    """Fail once with OSError, then succeed, to verify B3 retry handling."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("text must be a non-empty string")
    if not STATE_FILE.exists():
        STATE_FILE.write_text("failed_once", encoding="utf-8")
        raise OSError("temporary retry probe failure")
    return {"echo": text, "recovered": True}
