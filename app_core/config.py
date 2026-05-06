from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _pick(st_module: Any, key: str, default: Any = "") -> Any:
    try:
        if key in st_module.secrets:
            return st_module.secrets[key]
    except Exception:
        pass
    return os.getenv(key, default)


@dataclass
class AppConfig:
    project_id: str
    firebase_api_key: str
    gemini_api_key: str
    storage_bucket: str
    request_timeout_seconds: int = 30
    email_enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True
    email_from: str = ""
    email_bcc: list[str] = field(default_factory=list)


def load_config(st_module: Any) -> AppConfig:
    project_id = str(_pick(st_module, "PROJECT_ID", "site-departamento")).strip()
    storage_bucket = str(
        _pick(st_module, "STORAGE_BUCKET", f"{project_id}.firebasestorage.app")
    ).strip()

    bcc_raw = str(_pick(st_module, "EMAIL_BCC", "")).strip()
    email_bcc = [x.strip() for x in bcc_raw.split(",") if x.strip()]

    timeout_raw = _pick(st_module, "REQUEST_TIMEOUT_SECONDS", 30)
    try:
        timeout = int(timeout_raw)
    except Exception:
        timeout = 30

    return AppConfig(
        project_id=project_id,
        firebase_api_key=str(_pick(st_module, "FIREBASE_API_KEY", "")).strip(),
        gemini_api_key=str(_pick(st_module, "GEMINI_API_KEY", "")).strip(),
        storage_bucket=storage_bucket,
        request_timeout_seconds=max(5, timeout),
        email_enabled=_as_bool(_pick(st_module, "EMAIL_ENABLED", False)),
        smtp_host=str(_pick(st_module, "SMTP_HOST", "")).strip(),
        smtp_port=int(str(_pick(st_module, "SMTP_PORT", "587")).strip() or "587"),
        smtp_username=str(_pick(st_module, "SMTP_USERNAME", "")).strip(),
        smtp_password=str(_pick(st_module, "SMTP_PASSWORD", "")).strip(),
        smtp_use_tls=_as_bool(_pick(st_module, "SMTP_USE_TLS", True), default=True),
        email_from=str(_pick(st_module, "EMAIL_FROM", "")).strip(),
        email_bcc=email_bcc,
    )

