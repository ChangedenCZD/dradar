"""Interactive, local-only model-provider credential setup."""

from __future__ import annotations

import getpass
import os
import subprocess
import sys

from .providers import (
    DEEPSEEK_API_KEY_ENV,
    GROK_API_KEY_ENV,
    GROK_CLI_VERSION,
    deepseek_api_key,
    deepseek_credential_source,
    deepseek_secret_error,
    deepseek_secret_path,
    grok_auth_error,
    grok_auth_path,
    grok_cli_path,
    grok_home,
    parse_grok_cli_version,
    store_deepseek_api_key,
)


def cmd_provider_setup(args) -> int:
    """Read a DeepSeek key without echoing it or placing it in argv/history."""

    if args.provider == "grok":
        return _setup_grok_subscription()
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

    if args.provider == "grok":
        return _status_grok_subscription()
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


def _setup_grok_subscription() -> int:
    """Launch official device OAuth in a DRadar-owned GROK_HOME."""

    if not sys.stdin.isatty():
        print(
            "Grok subscription setup needs an interactive terminal. Run:\n"
            "  dradar provider setup grok\n"
            "This opens the official xAI device OAuth flow; no API key is accepted."
        )
        return 2
    executable = grok_cli_path()
    if not executable:
        print(
            "Official Grok CLI was not found. Install version "
            f"{GROK_CLI_VERSION}, then run `dradar provider setup grok` again."
        )
        return 1
    try:
        version = subprocess.run(
            [executable, "--version"], capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"could not verify Grok CLI: {exc}")
        return 1
    found_version = parse_grok_cli_version(version.stdout)
    if version.returncode != 0 or found_version != GROK_CLI_VERSION:
        print(
            f"Grok CLI {GROK_CLI_VERSION} is required; found "
            f"{found_version or 'unknown'}."
        )
        return 1
    home = grok_home()
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        os.chmod(home, 0o700)
    env = dict(os.environ)
    env["GROK_HOME"] = str(home)
    env.pop(GROK_API_KEY_ENV, None)
    print(
        "Starting official Grok device OAuth for the dedicated DRadar slot. "
        "Complete the browser/device prompt shown by Grok."
    )
    try:
        proc = subprocess.run([executable, "login", "--device-auth"], env=env)
    except OSError as exc:
        print(f"could not start Grok login: {exc}")
        return 1
    if proc.returncode != 0:
        print("Grok OAuth login did not complete successfully.")
        return proc.returncode or 1
    issue = grok_auth_error(grok_auth_path())
    if issue is not None:
        print(f"Grok login returned but the credential is not ready: {issue}")
        return 1
    print(
        f"Grok subscription OAuth is ready at {grok_auth_path()} (tokens hidden).\n"
        "The credential stays local and API-key authentication is disabled."
    )
    return 0


def _status_grok_subscription() -> int:
    executable = grok_cli_path()
    if not executable:
        print("Grok subscription provider not ready: official Grok CLI not found.")
        return 1
    try:
        proc = subprocess.run(
            [executable, "--version"], capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"Grok subscription provider not ready: {exc}")
        return 1
    found_version = parse_grok_cli_version(proc.stdout)
    if proc.returncode != 0 or found_version != GROK_CLI_VERSION:
        print(
            f"Grok subscription provider not ready: CLI {GROK_CLI_VERSION} "
            f"required, found {found_version or 'unknown'}."
        )
        return 1
    issue = grok_auth_error()
    if issue is not None:
        print(f"Grok subscription provider not ready: {issue}")
        return 1
    print(
        f"Grok subscription provider ready via {grok_auth_path()} "
        f"(OAuth tokens hidden, CLI {GROK_CLI_VERSION}, API keys disabled)."
    )
    return 0


__all__ = ["cmd_provider_setup", "cmd_provider_status"]
