"""Runtime configuration, read from environment variables."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass

BASE_URL = "https://api.rated.network"
KEYCHAIN_SERVICE = "rated-api-key"


def _bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _key_from_macos_keychain() -> str | None:
    """Fallback for macOS: read the key from the login Keychain.

    GUI-launched apps (e.g. the Claude desktop app) don't source ~/.zshrc, so an
    exported RATED_API_KEY may not reach this process. The Keychain item is
    created by the user with `security add-generic-password -s rated-api-key`.
    """
    if sys.platform != "darwin":
        return None
    try:
        result = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def resolve_api_key() -> tuple[str | None, str]:
    """Return (key, source). RATED_API_KEY always wins over the Keychain."""
    key = os.environ.get("RATED_API_KEY", "").strip()
    if key:
        return key, "RATED_API_KEY environment variable"
    key = _key_from_macos_keychain()
    if key:
        return key, f"macOS Keychain item '{KEYCHAIN_SERVICE}'"
    return None, ""


@dataclass(frozen=True)
class Settings:
    enable_writes: bool
    default_page_size: int
    max_page_size: int
    max_retries: int
    max_retry_wait: float
    min_request_interval: float
    timeout: float
    max_response_chars: int
    skip_key_check: bool

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            enable_writes=_bool("RATED_ENABLE_WRITES"),
            default_page_size=max(1, _int("RATED_DEFAULT_PAGE_SIZE", 5)),
            max_page_size=max(1, _int("RATED_MAX_PAGE_SIZE", 50)),
            max_retries=max(0, _int("RATED_MAX_RETRIES", 3)),
            max_retry_wait=_float("RATED_MAX_RETRY_WAIT", 30.0),
            # Free tier allows 2 requests/second.
            min_request_interval=_float("RATED_MIN_REQUEST_INTERVAL", 0.5),
            timeout=_float("RATED_TIMEOUT", 60.0),
            max_response_chars=_int("RATED_MAX_RESPONSE_CHARS", 60_000),
            skip_key_check=_bool("RATED_SKIP_KEY_CHECK"),
        )
