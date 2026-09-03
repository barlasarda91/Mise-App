"""Anthropic client + model alias resolution.

routines.model stores a friendly alias ('opus'/'sonnet') per spec §6; full
claude-* ids pass through untouched.
"""

import anthropic

MODEL_ALIASES = {
    "opus": "claude-opus-5",
    "sonnet": "claude-sonnet-5",
    "haiku": "claude-haiku-4-5",
}


def resolve_model(name: str) -> str:
    return MODEL_ALIASES.get(name, name)


def get_client() -> anthropic.Anthropic:
    # Reads ANTHROPIC_API_KEY from the environment.
    return anthropic.Anthropic()
