"""Interactive, local-only model-provider credential setup."""

from __future__ import annotations

import getpass
import sys

from .providers import (
    DEEPSEEK_API_KEY_ENV,
    deepseek_api_key,
    deepseek_credential_source,
    deepseek_secret_error,
    deepseek_secret_path,
    store_deepseek_api_key,
)


def cmd_provider_setup(args) -> int:
    """Read a DeepSeek key without echoing it or placing it in argv/history."""

    if args.provider != "deepseek":
        raise ValueError(f"unsupported provider: {args.provider}")
    if not sys.stdin.isatty():
        print(
            "DeepSeek setup needs an interactive terminal so the key can be "
            "entered with echo disabled. Open your own Terminal and run:\n"
            "  dradar provider setup deepseek\n"
            "Never paste the API key into Codex/chat or pass it as a command argument."
        )
        return 2
    key = getpass.getpass("DeepSeek API key (input hidden): ")
    try:
        path = store_deepseek_api_key(key)
    except (OSError, ValueError) as exc:
        print(f"could not save DeepSeek API key: {exc}")
        return 1
    print(
        f"DeepSeek API key saved locally at {path} (value hidden).\n"
        "It is not stored in config.json and is never sent to the DRadar server."
    )
    return 0


def cmd_provider_status(args) -> int:
    """Report credential readiness without printing secret material."""

    if args.provider != "deepseek":
        raise ValueError(f"unsupported provider: {args.provider}")
    path = deepseek_secret_path()
    error = deepseek_secret_error(path)
    if error is not None:
        print(f"DeepSeek provider not ready: {error}")
        return 1
    source = deepseek_credential_source()
    if source == "environment":
        print(f"DeepSeek provider ready via {DEEPSEEK_API_KEY_ENV} (value hidden).")
        return 0
    if source == "file" and deepseek_api_key():
        print(f"DeepSeek provider ready via {path} (value hidden).")
        return 0
    print(
        "DeepSeek provider not configured. In your own interactive Terminal run:\n"
        "  dradar provider setup deepseek"
    )
    return 1


__all__ = ["cmd_provider_setup", "cmd_provider_status"]
