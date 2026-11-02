"""Structured JSON logging with a hard redaction invariant (OBS-003 / OBS-013).

Every log line is a single JSON object. The blocklist is applied *unconditionally*
to :func:`log_event` — monitoring, tracing and structured logs never carry
evidence payload bytes, private keys, biometric embeddings, raw frame buffers
or sensitive case content, only references (IDs, digests) and status fields.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
from typing import Any, Mapping, Optional, Set, Union

__all__ = [
    "SENSITIVE_BLOCKLIST",
    "setup_structured_logging",
    "redact",
    "log_event",
]

_LOG_FORMAT = logging.Formatter('%(message)s')

_lock = threading.Lock()
_configured = False

#: Default blocklist (OBS-013 redaction invariant). Anything matching a
#: case-insensitive key or key-suffix is stripped from every structured log
#: event. Callers may add to this via ``redact(...)``'s ``secrets_keys`` or
#: by registering extra keys with :func:`register_sensitive_key`.
SENSITIVE_BLOCKLIST: Set[str] = frozenset(
    {
        # auth / credentials
        "password",
        "passwords",
        "passwd",
        "secret",
        "secrets",
        "api_key",
        "apikey",
        "api-key",
        "access_key",
        "secret_key",
        "private_key",
        "private_keys",
        "token",
        "tokens",
        "auth_token",
        "access_token",
        "refresh_token",
        "bearer_token",
        "jwt",
        "session_token",
        "credential",
        "credentials",
        "authorization",
        # cryptographic material
        "private_key_pem",
        "pem",
        "signature",
        "signatures",
        "ed25519_key",
        "signing_key",
        # biometric / case-sensitive payloads (never raw content — IDs only)
        "embedding",
        "embeddings",
        "biometric_embedding",
        "biometric_embeddings",
        "face_embedding",
        "face_embeddings",
        "speaker_embedding",
        "speaker_embeddings",
        "voiceprint",
        "voiceprints",
        "prnu_coefficients",
        # evidence / case payloads
        "evidence_payload",
        "evidence_payloads",
        "evidence_bytes",
        "frame_buffer",
        "frame_buffers",
        "audio_buffer",
        "video_buffer",
        "packet_dump",
        "quarantined_payload",
        "quarantined_payloads",
        "payload",
        "payloads",
        "raw_data",
        "case_content",
        "case_sensitive_content",
        # PII
        "pii",
        "pii_fields",
        "personal_data",
        "national_id",
        "nric",
        "ssn",
        "passport_number",
        "phone_number",
        "email_address",
        "full_name",
        "home_address",
    }
)

_registry_extra: Set[str] = set()


def register_sensitive_key(*keys: str) -> None:
    """Add extra key names to the blocklist at module-load time."""
    for k in keys:
        _registry_extra.add(k.lower().strip())


def _is_sensitive(key: str, extra: Optional[Set[str]]) -> bool:
    lk = key.lower()
    if lk in SENSITIVE_BLOCKLIST:
        return True
    for extra_set in (extra, _registry_extra):
        if extra_set and lk in extra_set:
            return True
    # Also strip keys that *end* in a sensitive suffix (e.g. ``"my_private_key"``).
    for suffix in (
        "_private_key",
        "_secret",
        "_token",
        "_password",
        "_embedding",
        "_embeddings",
        "_payload",
        "_payloads",
    ):
        if lk.endswith(suffix):
            return True
    return False


def redact(
    fields: Mapping[str, Any],
    secrets_keys: Optional[Union[Set[str], frozenset[str]]] = None,
) -> dict:
    """Return a copy of ``fields`` with every sensitive key stripped.

    ``secrets_keys`` adds caller-specific names (case-insensitive) on top of
    the module blocklist. Stripped keys are replaced with the marker
    ``"<redacted>"`` so log consumers can still see *that* a field existed.
    """
    out: dict = {}
    blocklist: Optional[Set[str]] = set(secrets_keys) if secrets_keys else None
    for key, value in fields.items():
        if _is_sensitive(str(key), blocklist):
            out[key] = "<redacted>"
        else:
            out[key] = value
    return out


class _JsonFormatter(logging.Formatter):
    """One JSON object per record; pre-redacted fields pass through, and a
    final belt-and-braces blocklist sweep runs over the assembled record so
    no raw exception text sneaks a sensitive key into a log line."""

    def format(self, record: logging.LogRecord) -> str:
        fields: dict = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "level": record.levelname,
            "logger": record.name,
        }
        if record.exc_info:
            fields["exception_type"] = record.exc_info[0].__name__
            # Intentionally not including the exception message — it may carry
            # a value that looks like evidence content or a key.
        msg = record.getMessage()
        if isinstance(msg, str):
            try:
                parsed = json.loads(msg)
                if isinstance(parsed, Mapping):
                    fields.update(parsed)
                else:
                    fields["event"] = msg
            except json.JSONDecodeError:
                fields["event"] = msg
        else:
            fields["event"] = msg
        return json.dumps(
            redact(fields), ensure_ascii=False, default=str, sort_keys=False
        )


def setup_structured_logging(level: Union[int, str] = logging.INFO) -> None:
    """Configure the root logger to emit one JSON line per event to stdout.

    Idempotent: a second call reconfigures the same handlers (no duplicates).
    Safe to call from workers at process start; it does not touch otel.
    """
    global _configured
    lvl = level if isinstance(level, int) else getattr(logging, str(level).upper(), logging.INFO)

    root = logging.getLogger()
    with _lock:
        if _configured:
            # Remove any prior structured-logging handlers we installed.
            for h in list(root.handlers):
                if getattr(h, "_ps05_structured", False):
                    root.removeHandler(h)
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(_JsonFormatter())
        handler._ps05_structured = True  # type: ignore[attr-defined]
        root.addHandler(handler)
        root.setLevel(lvl)
        # Silence noisy third-party chatter at the root; individual loggers
        # can opt back in.
        for noisy in ("urllib3", "httpx", "celery", "asyncio"):
            logging.getLogger(noisy).setLevel(max(lvl, logging.WARNING))
        _configured = True


def log_event(
    level: Union[int, str],
    event: str,
    **fields: Any,
) -> None:
    """Emit one structured JSON log event, auto-redacted against the blocklist.

    ``level`` is a logging level int or name (``"DEBUG"`` … ``"CRITICAL"``)
    or an int. ``event`` is the machine-readable event name. All keyword
    fields pass through :func:`redact` before emission — no sensitive value
    reaches the log line by default.
    """
    lvl_int = level if isinstance(level, int) else getattr(logging, str(level).upper(), logging.INFO)
    payload = redact(fields)
    record = logging.getLogger("ps05").makeRecord(
        name="ps05",
        level=lvl_int,
        fn=__file__,
        lno=0,
        msg=json.dumps({"event": event, **payload}, default=str, sort_keys=False),
        exc_info=None,
        extra={"ps05_event": event},
    )
    logging.getLogger("ps05").handle(record)
