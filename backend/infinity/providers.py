"""LLM provider settings: GUI-managed (encrypted in PostgreSQL) with .env as fallback."""

from __future__ import annotations

import logging
import time
from dataclasses import replace
from datetime import datetime, timezone
from urllib.parse import quote

import httpx
from sqlalchemy import select

from sqlalchemy.orm import Session

from app.core.config import Settings as LegacySettings
from app.core.exceptions import ConfigurationError
from app.generation.providers import create_llm_provider

from .config import get_config
from .crypto import SecretBox, SecretUnreadableError, mask_secret
from .models import AppSetting

log = logging.getLogger("infinity.providers")

KEY = "llm"
SUPPORTED = ("gemini", "openai", "ollama")


UNREADABLE_HINT = ("The AI provider credentials saved in the database cannot be decrypted with the current APP_SECRET_KEY "
                   "(they were saved with a different key). An administrator must re-enter the API key under Providers and press Save, "
                   "or restore the original APP_SECRET_KEY in .env.")


class ProviderCredentialsError(ConfigurationError):
    """Provider credentials are missing or unreadable. Mapped to a clear 409 response, never a 500."""


def _box() -> SecretBox:
    return SecretBox(get_config().secret_key)


def _read_secrets(row: AppSetting | None) -> tuple[dict, bool]:
    """Decrypt the stored provider secrets. Returns (secrets, unreadable). Never raises on a wrong key."""
    if not row or not row.secret_enc:
        return {}, False
    try:
        return _box().decrypt_json(row.secret_enc), False
    except SecretUnreadableError:
        return {}, True


def _archive_unreadable(row: AppSetting, value: dict) -> None:
    """Keep the old, still-encrypted blob (opaque, never decrypted or exposed) so nothing is destroyed: if the original
    APP_SECRET_KEY is ever found it can still be recovered from here."""
    archive = list(value.get("archived_secrets", []))
    archive.append({"token": row.secret_enc, "archived_at": datetime.now(timezone.utc).isoformat()})
    value["archived_secrets"] = archive[-20:]


def effective(session: Session) -> dict:
    """Merge .env defaults with GUI-saved settings (GUI wins). Includes secrets - never return directly."""
    cfg = get_config()
    result = {
        "provider": cfg.llm_provider,
        "models": {"gemini": cfg.gemini_model, "openai": cfg.llm_model if cfg.llm_provider == "openai" else "gpt-4o-mini", "ollama": cfg.llm_model},
        "ollama_base_url": cfg.ollama_base_url,
        "secrets": {"gemini": cfg.gemini_api_key, "openai": cfg.openai_api_key},
        "source": "environment",
        "credentials_unreadable": False,
    }
    row = session.get(AppSetting, KEY)
    if row:
        saved = row.value or {}
        result["provider"] = saved.get("provider", result["provider"])
        result["models"].update(saved.get("models", {}))
        result["ollama_base_url"] = saved.get("ollama_base_url", result["ollama_base_url"])
        stored, unreadable = _read_secrets(row)
        for name, value in stored.items():
            if value:
                result["secrets"][name] = value
        if unreadable:
            # Do NOT pretend the stored keys exist: flag it so callers/UI can say exactly what is wrong.
            result["credentials_unreadable"] = True
            log.warning("stored provider credentials cannot be decrypted with the current APP_SECRET_KEY; re-enter them under Providers")
        result["source"] = "gui"
    return result


def public_view(session: Session) -> dict:
    eff = effective(session)
    return {
        "provider": eff["provider"],
        "models": eff["models"],
        "ollama_base_url": eff["ollama_base_url"],
        "keys": {name: {"configured": bool(v), "preview": mask_secret(v)} for name, v in eff["secrets"].items()},
        "source": eff["source"],
        "supported": list(SUPPORTED),
        "credentials_unreadable": eff["credentials_unreadable"],
        "message": UNREADABLE_HINT if eff["credentials_unreadable"] else None,
    }


def save(session: Session, *, provider: str | None, models: dict | None, ollama_base_url: str | None, keys: dict | None) -> None:
    if provider is not None and provider not in SUPPORTED:
        raise ValueError(f"provider must be one of {', '.join(SUPPORTED)}")
    row = session.get(AppSetting, KEY) or AppSetting(key=KEY, value={})
    value = dict(row.value or {})
    if provider is not None:
        value["provider"] = provider
    if models:
        value["models"] = {**value.get("models", {}), **{k: v.strip() for k, v in models.items() if k in SUPPORTED and v and v.strip()}}
    if ollama_base_url:
        value["ollama_base_url"] = ollama_base_url.strip()
    secrets, unreadable = _read_secrets(row)
    changes = {n: v for n, v in (keys or {}).items() if n in {"gemini", "openai"} and v is not None}
    if unreadable and not changes:
        # Nothing new to store: leave the old encrypted record untouched (it may become readable again with the right key).
        row.value = value
        session.merge(row)
        session.flush()
        return
    if unreadable:
        log.warning("replacing unreadable stored provider credentials with newly entered ones")
        _archive_unreadable(row, value)
    for name, secret in (keys or {}).items():
        if name in {"gemini", "openai"} and secret is not None:
            if secret == "":
                secrets.pop(name, None)  # explicit clear
            else:
                secrets[name] = secret.strip()
    row.value = value
    row.secret_enc = _box().encrypt_json(secrets) if secrets else None
    session.merge(row)
    session.flush()


# ----------------------------------------------------------------------------------------------------------------------
# Explicit credential replacement (works even when the stored credential can no longer be decrypted)
# ----------------------------------------------------------------------------------------------------------------------
class CredentialRejected(ValueError):
    """The provider says the submitted key is invalid (nothing is stored)."""


class ValidationUnavailable(RuntimeError):
    """The provider could not be reached to validate the key (nothing is stored unless the admin skips validation)."""


def _http_get(url: str, headers: dict, timeout: float = 12.0) -> tuple[int, dict]:
    """Single outbound validation call. Keys travel in headers only (never in the URL), so request logging cannot leak them."""
    response = httpx.get(url, headers=headers, timeout=timeout, follow_redirects=False)
    try:
        body = response.json()
    except ValueError:
        body = {}
    return response.status_code, body if isinstance(body, dict) else {}


def validate_key(provider: str, api_key: str, model: str | None = None) -> dict:
    """Validate a NEW key with the provider's own read-only API (no tokens are generated, the old key is not involved).

    Returns {"status": "valid", "model_warning": str|None}. Raises CredentialRejected (invalid key) or ValidationUnavailable.
    Gemini:  GET /v1beta/models/{model}  - an invalid key is rejected with 400/403 before the model is looked up, so a 404
             means the key is fine but the configured model name does not exist (reported as a warning).
    OpenAI:  GET /v1/models.
    """
    try:
        if provider == "gemini":
            model_id = (model or "").removeprefix("models/") or "gemini-flash-latest"
            status, body = _http_get(f"https://generativelanguage.googleapis.com/v1beta/models/{quote(model_id, safe='')}",
                                     {"x-goog-api-key": api_key})
            if status == 200:
                return {"status": "valid", "model_warning": None}
            if status == 404:
                return {"status": "valid", "model_warning": f"The key is accepted, but the model “{model_id}” was not found by Google. Check the model name."}
            if status in (400, 401, 403):
                reason = (body.get("error") or {}).get("message", "")
                raise CredentialRejected("Google rejected this API key" + (f": {reason[:120]}" if reason and api_key not in reason else "."))
            if status == 429:
                return {"status": "valid", "model_warning": "The key is accepted, but Google reports the quota is currently exhausted."}
            raise ValidationUnavailable(f"Google returned HTTP {status} while validating the key.")
        if provider == "openai":
            status, body = _http_get("https://api.openai.com/v1/models", {"Authorization": f"Bearer {api_key}"})
            if status == 200:
                return {"status": "valid", "model_warning": None}
            if status in (401, 403):
                raise CredentialRejected("OpenAI rejected this API key.")
            if status == 429:
                return {"status": "valid", "model_warning": "The key is accepted, but OpenAI reports the quota is currently exhausted."}
            raise ValidationUnavailable(f"OpenAI returned HTTP {status} while validating the key.")
    except httpx.HTTPError as error:
        raise ValidationUnavailable(f"Could not reach the provider to validate the key ({type(error).__name__}).") from None
    raise ValueError(f"cannot validate keys for provider {provider!r}")


def replace_credential(session: Session, provider: str, api_key: str) -> dict:
    """Store ONE provider key, encrypted with the CURRENT APP_SECRET_KEY, without needing the previous value.

    - The previous encrypted blob is never decrypted when it is unreadable; it is archived (still encrypted) in the same row.
    - If the previous blob IS readable, the other provider's key is carried over; only the selected one changes.
    - Provider/model/base-URL settings and the sources table are untouched. The caller commits (single transaction).
    """
    if provider not in {"gemini", "openai"}:
        raise ValueError("provider must be gemini or openai")
    api_key = (api_key or "").strip()
    if len(api_key) < 8:
        raise ValueError("The API key looks too short.")
    row = session.execute(select(AppSetting).where(AppSetting.key == KEY).with_for_update()).scalar_one_or_none()  # lock: no lost updates
    if row is None:
        row = AppSetting(key=KEY, value={})
        session.add(row)
    secrets, unreadable = _read_secrets(row)
    value = dict(row.value or {})
    if unreadable:
        _archive_unreadable(row, value)
        secrets = {}
    secrets[provider] = api_key
    row.value = value
    row.secret_enc = _box().encrypt_json(secrets)
    session.flush()
    return {"archived_previous": unreadable, "kept_other_providers": sorted(k for k in secrets if k != provider)}


def build_provider(session: Session, override_provider: str | None = None):
    eff = effective(session)
    provider = override_provider or eff["provider"]
    model = eff["models"].get(provider, "")
    if provider in {"gemini", "openai"} and not eff["secrets"].get(provider):
        label = {"gemini": "Gemini", "openai": "OpenAI"}[provider]
        if eff["credentials_unreadable"]:
            raise ProviderCredentialsError(f"{UNREADABLE_HINT} (Needed now: the {label} API key.)")
        raise ProviderCredentialsError(f"No {label} API key is configured. An administrator can add it under Providers.")
    legacy = replace(
        LegacySettings(),
        llm_provider=provider,
        llm_model=model,
        gemini_model=eff["models"].get("gemini", ""),
        gemini_api_key=eff["secrets"].get("gemini"),
        openai_api_key=eff["secrets"].get("openai"),
        ollama_base_url=eff["ollama_base_url"],
    )
    return create_llm_provider(legacy), provider, model


def test_connection(session: Session, provider: str | None = None) -> dict:
    started = time.monotonic()
    try:
        llm, name, model = build_provider(session, provider)
        text = llm.complete("Reply with the single word: OK")
        return {"ok": True, "provider": name, "model": model, "latency_ms": int((time.monotonic() - started) * 1000), "reply": text[:80]}
    except ProviderCredentialsError as error:
        return {"ok": False, "provider": provider, "error": str(error), "code": "credentials"}
    except Exception as error:  # surface a safe message, never the key
        return {"ok": False, "provider": provider, "error": f"{type(error).__name__}: {str(error)[:200]}"}
