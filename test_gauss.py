"""Smoke test for the Samsung Gauss-O4 LLM endpoint.

Usage:
    uv run test_gauss.py
    uv run test_gauss.py "Translate to English: 안녕하세요"

Reads GAUSS_* variables from .env (same ones batch.py / main.py --gauss use):

    GAUSS_BASE_URL     full https URL to the Gauss endpoint
    GAUSS_API_KEY      bearer key
    GAUSS_CREDENTIAL   x-dep-ticket value
    GAUSS_USER_ID      KNOX ID
    GAUSS_SYSTEM_NAME  optional (default: patent_translator)
    GAUSS_USER_TYPE    optional (default: AD_ID)
    GAUSS_MODEL        optional (default: GaussO4.1-260330)

Prints the effective config (with the credential / api key masked), makes one
chat.completions call, and prints the response. Exits non-zero on any error.
"""

from __future__ import annotations

import sys

from translate.client import ClientConfig, LLMClient


def _mask(value: str) -> str:
    if not value:
        return "<empty>"
    if len(value) <= 6:
        return "*" * len(value)
    return f"{value[:3]}…{value[-3:]} ({len(value)} chars)"


def main() -> int:
    user_prompt = sys.argv[1] if len(sys.argv) > 1 else "Say hello in one short sentence."

    try:
        config = ClientConfig.from_env(provider="gauss")
    except Exception as exc:
        print(f"Could not load Gauss config from .env: {type(exc).__name__}: {exc}")
        return 1

    print("=== Gauss endpoint config ===")
    print(f"  model            : {config.model}")
    print(f"  base_url         : {config.base_url or '<empty>'}")
    print(f"  api_key          : {_mask(config.api_key)}")
    print(f"  x-dep-ticket     : {_mask(config.gauss_credential)}")
    print(f"  Send-System-Name : {config.gauss_system_name}")
    print(f"  User-Id          : {config.gauss_user_id or '<empty>'}")
    print(f"  User-Type        : {config.gauss_user_type}")
    print(f"  temperature      : {config.temperature}")
    print(f"  max_tokens       : {config.max_tokens}")
    print()

    try:
        client = LLMClient(config)
    except RuntimeError as exc:
        print(f"Gauss client construction failed: {exc}")
        print("Fill the missing values in .env (see .env.example) and try again.")
        return 1

    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user",   "content": user_prompt},
    ]
    print(f"=== Calling {config.model} ===")
    print(f"  user prompt: {user_prompt!r}")
    print()

    try:
        reply = client.complete(messages)
    except Exception as exc:
        print(f"Call failed: {type(exc).__name__}: {exc}")
        return 1

    print("=== Response ===")
    print(reply)
    return 0


if __name__ == "__main__":
    sys.exit(main())
