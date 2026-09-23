"""Anthropic client + model alias resolution.

routines.model stores a friendly alias ('opus'/'sonnet') per spec §6; full
claude-* ids pass through untouched.

Resolution is retirement-proof: the alias's pinned id is used while Anthropic
still serves it, and when it's retired the alias falls back to the LOWEST-
versioned model still available in the same family (per Arda: the exact point
release doesn't matter to the portal, and cheapest-available beats newest).
Availability comes from the live Models API, cached; any trouble reading it
fails open to the pinned id so this can never take a run down on its own.
"""

import logging
import re
import time

import anthropic

log = logging.getLogger(__name__)

MODEL_ALIASES = {
    "opus": "claude-opus-5",
    "sonnet": "claude-sonnet-5",
    "haiku": "claude-haiku-4-5",
}

AVAILABLE_TTL_SECONDS = 6 * 3600
_available_cache: dict = {"at": 0.0, "ids": None}


def _available_model_ids() -> set[str] | None:
    """Model ids the API currently serves; None when never fetched. A failed
    refresh keeps serving the last-known set (stale beats broken)."""
    now = time.time()
    if _available_cache["ids"] is not None and now - _available_cache["at"] < AVAILABLE_TTL_SECONDS:
        return _available_cache["ids"]
    try:
        ids = {m.id for m in get_client().models.list()}
    except Exception as exc:
        log.warning("models list unavailable (%s) — using pinned model ids", exc)
        return _available_cache["ids"]
    _available_cache.update(at=now, ids=ids)
    return ids


def _family_of(model_id: str) -> str | None:
    match = re.match(r"claude-([a-z]+)-", model_id)
    return match.group(1) if match else None


def _version_key(model_id: str) -> list[int]:
    return [int(part) for part in re.findall(r"\d+", model_id)]


def resolve_model(name: str) -> str:
    preferred = MODEL_ALIASES.get(name, name)
    available = _available_model_ids()
    if available is None or preferred in available:
        return preferred
    family = _family_of(preferred)
    if not family:
        return preferred
    candidates = sorted(
        (mid for mid in available if mid.startswith(f"claude-{family}-")),
        key=_version_key,
    )
    if candidates:
        log.warning("model %s is not served anymore — using %s instead", preferred, candidates[0])
        return candidates[0]
    return preferred  # family gone entirely — let the API say so explicitly


def get_client() -> anthropic.Anthropic:
    # Reads ANTHROPIC_API_KEY from the environment.
    return anthropic.Anthropic()
