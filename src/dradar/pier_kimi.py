"""Credential-isolated Pier adapter for the official Kimi Code subscription CLI.

Only Kimi's managed OAuth service is supported.  The adapter installs the
exact public Kimi CLI release in the task image and injects one locked run-copy
of the credential; API-key providers and ambient Kimi settings are deliberately
excluded.
"""

from __future__ import annotations

import json
import os
import shlex
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

from pier.agents.installed.base import BaseInstalledAgent, with_prompt_template
from pier.environments.base import BaseEnvironment
from pier.models.agent.context import AgentContext
from pier.models.agent.install import AgentInstallSpec, InstallStep
from pier.models.agent.network import NetworkAllowlist
from pier.models.trajectories import Agent, FinalMetrics, Step, Trajectory
from pier.utils.trajectory_metrics import populate_context_from_final_metrics

from _dradar_kimi_recovery import run_with_kimi_resume, validated_session_id


KIMI_CONFIG = """\
default_model = "kimi-code/k3"
default_thinking = true
default_yolo = true
default_plan_mode = false
merge_all_available_skills = false
telemetry = false

[providers."managed:kimi-code"]
type = "kimi"
api_key = ""
base_url = "https://api.kimi.com/coding/v1"

[providers."managed:kimi-code".oauth]
storage = "file"
key = "oauth/kimi-code"

[models."kimi-code/k3"]
provider = "managed:kimi-code"
model = "k3"
max_context_size = 1048576
capabilities = ["thinking", "always_thinking"]
display_name = "K3"

[background]
keep_alive_on_exit = false

[[hooks]]
event = "PreToolUse"
matcher = "Shell|ReadFile|ReadMediaFile|Glob|Grep|WriteFile|StrReplaceFile"
command = "/opt/kimi-runtime/tools/kimi-cli/bin/python /tmp/dradar-kimi-policy.py"
timeout = 5
"""

KIMI_AGENT_SPEC = """\
version: 1
agent:
  extend: default
  name: dradar-kimi-code
  allowed_tools:
    - "kimi_cli.tools.shell:Shell"
    - "kimi_cli.tools.file:ReadFile"
    - "kimi_cli.tools.file:ReadMediaFile"
    - "kimi_cli.tools.file:Glob"
    - "kimi_cli.tools.file:Grep"
    - "kimi_cli.tools.file:WriteFile"
    - "kimi_cli.tools.file:StrReplaceFile"
  subagents: {}
"""

KIMI_POLICY = r'''#!/usr/bin/env python3
import json
import sys

PROTECTED = (
    "/tmp/dradar-kimi-home",
    "KIMI_SHARE_DIR",
    "KIMI_CODE_HOME",
    "credentials/kimi-code.json",
    "oauth/kimi-code",
)

try:
    request = json.load(sys.stdin)
except Exception:
    print("Kimi policy could not validate the tool request", file=sys.stderr)
    raise SystemExit(2)

tool_input = json.dumps(request.get("tool_input", {}), ensure_ascii=False)
if any(marker in tool_input for marker in PROTECTED):
    print("Access to the isolated Kimi credential area is denied", file=sys.stderr)
    raise SystemExit(2)
'''

KIMI_LAUNCHER = r'''#!/usr/bin/env python3
import os

import aiohttp

# Kimi CLI's OAuth refresh transport does not currently opt in to the
# standard proxy environment.  Pier deliberately exposes outbound access
# through those variables, so make the official refresh client honour the
# same interface as Kimi's model client.  This keeps refresh working during
# long runs without assuming a host proxy product, address, or port.
_original_client_session = aiohttp.ClientSession


def _proxy_aware_client_session(*args, **kwargs):
    kwargs.setdefault("trust_env", True)
    return _original_client_session(*args, **kwargs)


aiohttp.ClientSession = _proxy_aware_client_session

from kimi_cli import llm as kimi_llm

effort = os.environ.get("KIMI_MODEL_THINKING_EFFORT", "")
if effort not in {"low", "high", "max"}:
    raise SystemExit("invalid DRadar Kimi reasoning effort")

original_create_llm = kimi_llm.create_llm


def create_llm_with_effort(*args, **kwargs):
    llm = original_create_llm(*args, **kwargs)
    if llm is None:
        return None
    provider = kimi_llm.find_kimi_provider(llm.chat_provider)
    if provider is None:
        raise RuntimeError("DRadar Kimi launcher requires the official Kimi provider")
    # Kimi CLI 1.49 configures thinking.type but deliberately stopped adding
    # the legacy reasoning_effort field.  K3's subscription endpoint still
    # exposes DRadar's low/high/max lanes through the documented explicit
    # generation-kwargs passthrough.
    llm.chat_provider = provider.with_thinking(effort).with_generation_kwargs(
        reasoning_effort=effort
    )
    return llm


kimi_llm.create_llm = create_llm_with_effort

from kimi_cli.__main__ import main

raise SystemExit(main())
'''

KIMI_CLI_VERSION = "1.49.0"
UV_VERSION = "0.9.7"
UV_SHA256 = {
    "x86_64": "b26fcc8dfa1c39b5a5613445af3be3eefda45d9a39359bee271eafe34913583e",
    "aarch64": "8b3d31a154673c6d357727d2083a33525b515589d153fa5b5455e1db9e9e6363",
}


def _install_command() -> str:
    return (
        "set -euo pipefail; "
        "if [ -f /etc/alpine-release ] || ldd --version 2>&1 | grep -qi musl; then "
        "  echo 'Kimi Code requires a glibc task image' >&2; exit 1; "
        "elif command -v apt-get >/dev/null 2>&1; then "
        "  apt-get update && DEBIAN_FRONTEND=noninteractive "
        "  apt-get install -y --no-install-recommends ca-certificates curl tar; "
        "elif command -v dnf >/dev/null 2>&1; then "
        "  dnf install -y ca-certificates curl tar gzip; "
        "elif command -v yum >/dev/null 2>&1; then "
        "  yum install -y ca-certificates curl tar gzip; "
        "else echo 'No supported package manager found' >&2; exit 1; fi; "
        'case "$(uname -m)" in '
        f"  x86_64) uv_arch=x86_64; uv_sha={UV_SHA256['x86_64']} ;; "
        f"  aarch64|arm64) uv_arch=aarch64; uv_sha={UV_SHA256['aarch64']} ;; "
        "  *) echo 'Unsupported CPU architecture' >&2; exit 1 ;; "
        "esac; "
        f"uv_archive=/tmp/uv-{UV_VERSION}.tar.gz; "
        f"uv_url=https://github.com/astral-sh/uv/releases/download/{UV_VERSION}/"
        'uv-${uv_arch}-unknown-linux-gnu.tar.gz; '
        "curl --fail --silent --show-error --location "
        '  --output "${uv_archive}" "${uv_url}"; '
        'printf \'%s  %s\\n\' "${uv_sha}" "${uv_archive}" '
        "  | sha256sum --check --strict -; "
        "mkdir -p /opt/kimi-runtime/bin /opt/kimi-runtime/python "
        "  /opt/kimi-runtime/tools /tmp/dradar-uv; "
        'tar -xzf "${uv_archive}" -C /tmp/dradar-uv; '
        'install -m 0755 "/tmp/dradar-uv/uv-${uv_arch}-unknown-linux-gnu/uv" '
        "  /opt/kimi-runtime/bin/uv; "
        "UV_TOOL_DIR=/opt/kimi-runtime/tools "
        "UV_TOOL_BIN_DIR=/opt/kimi-runtime/bin "
        "UV_PYTHON_INSTALL_DIR=/opt/kimi-runtime/python "
        "/opt/kimi-runtime/bin/uv tool install --python 3.13 "
        f"  'kimi-cli=={KIMI_CLI_VERSION}'; "
        f"test \"$(/opt/kimi-runtime/bin/kimi --version)\" = 'kimi, version {KIMI_CLI_VERSION}'"
    )


class KimiCode(BaseInstalledAgent):
    """Run pinned Kimi K3 headlessly with an isolated OAuth data root."""

    SUPPORTS_ATIF = True
    _REMOTE_HOME = PurePosixPath("/tmp/dradar-kimi-home")
    _REMOTE_USER_HOME = PurePosixPath("/tmp/dradar-kimi-user")
    _REMOTE_AUTH = _REMOTE_HOME / "credentials" / "kimi-code.json"
    _REMOTE_OAUTH_LOCK = _REMOTE_HOME / "oauth" / "kimi-code"
    _REMOTE_CONFIG = _REMOTE_HOME / "config.toml"
    _REMOTE_CLI = PurePosixPath("/opt/kimi-runtime/bin/kimi")
    _REMOTE_PYTHON = PurePosixPath(
        "/opt/kimi-runtime/tools/kimi-cli/bin/python"
    )
    _REMOTE_AGENT_SPEC = PurePosixPath("/tmp/dradar-kimi-agent.yaml")
    _REMOTE_POLICY = PurePosixPath("/tmp/dradar-kimi-policy.py")
    _REMOTE_LAUNCHER = PurePosixPath("/tmp/dradar-kimi-launcher.py")
    _REMOTE_SKILLS = PurePosixPath("/tmp/dradar-kimi-empty-skills")
    _STREAM_FILE = "kimi-code.jsonl"
    _SESSION_LOG_FILE = "kimi-code-session.log"

    @staticmethod
    def name() -> str:
        return "kimi-code"

    def __init__(
        self,
        *args: Any,
        auth_json_file: str,
        kimi_cli_file: str,
        reasoning_effort: str,
        **kwargs: Any,
    ):
        auth = Path(auth_json_file)
        if not auth.is_file():
            raise ValueError("Kimi OAuth run credential is missing")
        cli = Path(kimi_cli_file)
        if not cli.is_file():
            raise ValueError("Verified host Kimi CLI executable is missing")
        if reasoning_effort not in {"low", "high", "max"}:
            raise ValueError("Kimi reasoning_effort must be low, high, or max")
        try:
            payload = json.loads(auth.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Kimi OAuth run credential is invalid") from exc
        secrets = {
            payload.get(name)
            for name in ("access_token", "refresh_token")
            if isinstance(payload.get(name), str) and payload[name]
        }
        if len(secrets) < 2:
            raise ValueError("Kimi OAuth run credential is not refreshable")
        self._auth_json_file = auth
        self._reasoning_effort = reasoning_effort
        self._credential_values = secrets
        self._instruction = ""
        self._resume_attempts = 0
        self._session_id: str | None = None
        super().__init__(*args, **kwargs)

    def get_version_command(self) -> str:
        return f"{self._REMOTE_CLI.as_posix()} --version"

    def install_spec(self) -> AgentInstallSpec:
        version = self._version or KIMI_CLI_VERSION
        return AgentInstallSpec(
            agent_name=self.name(),
            version=version,
            steps=[InstallStep(user="root", run=_install_command())],
            verification_command=(
                f"test \"$({self._REMOTE_CLI.as_posix()} --version)\" = "
                f"'kimi, version {KIMI_CLI_VERSION}'"
            ),
            cache_key=(
                f"dradar-kimi-code-{version}-uv-{UV_VERSION}-runtime-v1"
            ),
        )

    def network_allowlist(self) -> NetworkAllowlist:
        return NetworkAllowlist(domains=["auth.kimi.com", "api.kimi.com"])

    @with_prompt_template
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        del context
        self._instruction = instruction
        remote_home = self._REMOTE_HOME.as_posix()
        remote_user_home = self._REMOTE_USER_HOME.as_posix()
        remote_auth = self._REMOTE_AUTH.as_posix()
        remote_lock = self._REMOTE_OAUTH_LOCK.as_posix()
        remote_config = self._REMOTE_CONFIG.as_posix()
        remote_cli = self._REMOTE_CLI.as_posix()
        remote_python = self._REMOTE_PYTHON.as_posix()
        remote_agent_spec = self._REMOTE_AGENT_SPEC.as_posix()
        remote_policy = self._REMOTE_POLICY.as_posix()
        remote_launcher = self._REMOTE_LAUNCHER.as_posix()
        remote_skills = self._REMOTE_SKILLS.as_posix()
        stream = f"/logs/agent/{self._STREAM_FILE}"
        session_log = f"/logs/agent/{self._SESSION_LOG_FILE}"
        env = self.build_process_env({
            "HOME": remote_user_home,
            "KIMI_SHARE_DIR": remote_home,
            "KIMI_DISABLE_TELEMETRY": "1",
            "KIMI_CODE_NO_AUTO_UPDATE": "1",
            "KIMI_CLI_NO_AUTO_UPDATE": "1",
            "KIMI_DISABLE_CRON": "1",
            "KIMI_CODE_AGENT_SWARM_MAX_CONCURRENCY": "1",
            "KIMI_MODEL_THINKING_EFFORT": self._reasoning_effort,
        })
        for name in (
            "KIMI_API_KEY",
            "KIMI_MODEL_API_KEY",
            "MOONSHOT_API_KEY",
            "KIMI_CODE_PASSWORD",
        ):
            env.pop(name, None)
        await self.exec_as_agent(
            environment,
            command=(
                f"mkdir -p {shlex.quote(remote_home + '/credentials')} "
                f"{shlex.quote(remote_home + '/oauth')} "
                f"{shlex.quote(remote_user_home)} "
                f"{shlex.quote(remote_skills)} "
                f"&& chmod 700 {shlex.quote(remote_home)} "
                f"{shlex.quote(remote_home + '/credentials')} "
                f"{shlex.quote(remote_home + '/oauth')} "
                f"{shlex.quote(remote_user_home)} "
                f"{shlex.quote(remote_skills)} "
                f"&& : > {shlex.quote(remote_lock)}"
            ),
            env=env,
        )
        local_config = self.logs_dir / "kimi-config.toml"
        local_agent_spec = self.logs_dir / "kimi-agent.yaml"
        local_policy = self.logs_dir / "kimi-policy.py"
        local_launcher = self.logs_dir / "kimi-launcher.py"
        local_config.write_text(KIMI_CONFIG, encoding="utf-8")
        local_agent_spec.write_text(KIMI_AGENT_SPEC, encoding="utf-8")
        local_policy.write_text(KIMI_POLICY, encoding="utf-8")
        local_launcher.write_text(KIMI_LAUNCHER, encoding="utf-8")
        await environment.upload_file(self._auth_json_file, remote_auth)
        await environment.upload_file(local_config, remote_config)
        await environment.upload_file(local_agent_spec, remote_agent_spec)
        await environment.upload_file(local_policy, remote_policy)
        await environment.upload_file(local_launcher, remote_launcher)
        targets = " ".join(
            shlex.quote(value)
            for value in (
                remote_auth,
                remote_lock,
                remote_config,
                remote_agent_spec,
                remote_policy,
                remote_launcher,
            )
        )
        if environment.default_user is not None:
            await self.exec_as_root(
                environment,
                command=(
                    f"chown {shlex.quote(str(environment.default_user))} {targets} "
                    f"&& chmod 600 {shlex.quote(remote_auth)} "
                    f"{shlex.quote(remote_lock)} {shlex.quote(remote_config)} "
                    f"{shlex.quote(remote_agent_spec)} "
                    f"&& chmod 500 {shlex.quote(remote_policy)} "
                    f"{shlex.quote(remote_launcher)}"
                ),
                env=env,
            )
        else:
            await self.exec_as_agent(
                environment,
                command=(
                    f"chmod 600 {shlex.quote(remote_auth)} "
                    f"{shlex.quote(remote_lock)} {shlex.quote(remote_config)} "
                    f"{shlex.quote(remote_agent_spec)} "
                    f"&& chmod 500 {shlex.quote(remote_policy)} "
                    f"{shlex.quote(remote_launcher)}"
                ),
                env=env,
            )
        version = self._version or KIMI_CLI_VERSION
        version_pattern = version.replace(".", r"\.")
        await self.exec_as_agent(
            environment,
            command=(
                f"{shlex.quote(remote_cli)} --version "
                f"| grep -Eq '(^| ){version_pattern}( |$)'"
            ),
            env=env,
        )
        common_flags = [
            "--print",
            "--yolo",
            "--model", "kimi-code/k3",
            "--output-format", "stream-json",
            "--config-file", remote_config,
            "--work-dir", "/app",
            "--agent-file", remote_agent_spec,
            "--skills-dir", remote_skills,
        ]

        def command_for(extra_flags: list[str], *, append: bool) -> str:
            flags = [*common_flags, *extra_flags]
            cli = " ".join(shlex.quote(part) for part in flags)
            tee = "tee -a" if append else "tee"
            return (
                "bash -o pipefail -c "
                + shlex.quote(
                    f"{remote_python} {remote_launcher} {cli} 2>&1 "
                    f"| {tee} {stream}"
                )
            )

        async def remote_session_id(*, copy_log: bool = False) -> str | None:
            copy = (
                f"cp \"$candidate\" {shlex.quote(session_log)}; "
                if copy_log else ""
            )
            result = await self.exec_as_agent(
                environment,
                command=(
                    "candidate=$(find " + shlex.quote(remote_home + "/sessions")
                    + " -type f -name 'wire.jsonl' -print 2>/dev/null "
                    "| tail -n 1); "
                    f"if [ -n \"$candidate\" ]; then {copy}"
                    "basename \"$(dirname \"$candidate\")\"; fi"
                ),
                env=env,
            )
            return validated_session_id(result.stdout)

        async def run_initial() -> None:
            await self.exec_as_agent(
                environment,
                command=command_for(["--prompt", instruction], append=False),
                env=env,
            )

        async def run_resume(session_id: str, prompt: str) -> None:
            await self.exec_as_agent(
                environment,
                command=command_for(
                    ["--session", session_id, "--prompt", prompt], append=True,
                ),
                env=env,
            )

        def announce_retry(attempt: int, delay: float, session_id: str) -> None:
            self.logger.warning(
                "Kimi temporary provider failure; resuming session %s in %ss "
                "(attempt %s/2)",
                session_id,
                int(delay),
                attempt,
            )

        try:
            self._resume_attempts, self._session_id = await run_with_kimi_resume(
                run_initial=run_initial,
                find_session_id=remote_session_id,
                run_resume=run_resume,
                on_retry=announce_retry,
            )
        finally:
            try:
                recovered_session_id = await remote_session_id(copy_log=True)
                if recovered_session_id is not None:
                    self._session_id = recovered_session_id
            except Exception as exc:
                self.logger.warning("Could not recover Kimi session log: %s", exc)
            try:
                await environment.download_file(remote_auth, self._auth_json_file)
                if os.name != "nt":
                    os.chmod(self._auth_json_file, 0o600)
            except Exception as exc:
                self.logger.warning("Could not recover refreshed Kimi OAuth state: %s", exc)

    def _redact_or_reject_credential_output(self, paths: list[Path]) -> None:
        leaked = False
        for path in paths:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            redacted = text
            for value in self._credential_values:
                if value in redacted:
                    leaked = True
                    redacted = redacted.replace(value, "[REDACTED_KIMI_CREDENTIAL]")
            if redacted != text:
                path.write_text(redacted, encoding="utf-8")
        if leaked:
            raise ValueError(
                "Kimi credential material reached agent output; logs were redacted "
                "and the run was rejected"
            )

    @staticmethod
    def _content_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return ""
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            text = block.get("text")
            if block.get("type") == "text" and isinstance(text, str):
                parts.append(text)
        return "\n\n".join(part for part in parts if part)

    def populate_context_post_run(self, context: AgentContext) -> None:
        stream_path = self.logs_dir / self._STREAM_FILE
        session_log_path = self.logs_dir / self._SESSION_LOG_FILE
        self._redact_or_reject_credential_output([stream_path, session_log_path])
        try:
            lines = stream_path.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
        except OSError:
            return
        steps: list[Step] = []
        if self._instruction:
            steps.append(
                Step(step_id=1, source="user", message=self._instruction)
            )
        session_id: str | None = None
        assistant_calls = 0
        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "session.resume_hint":
                value = event.get("session_id")
                if isinstance(value, str):
                    session_id = value
                continue
            if event.get("role") != "assistant":
                continue
            content = self._content_text(event.get("content"))
            if not content:
                continue
            assistant_calls += 1
            steps.append(
                Step(
                    step_id=len(steps) + 1,
                    source="agent",
                    message=content,
                    model_name=self.model_name,
                    reasoning_effort=self._reasoning_effort,
                    llm_call_count=1,
                )
            )
        if assistant_calls == 0:
            return
        output_tokens = 0
        try:
            wire_lines = session_log_path.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
        except OSError:
            wire_lines = []
        for line in wire_lines:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            message = record.get("message")
            if not isinstance(message, dict) or message.get("type") != "StatusUpdate":
                continue
            payload = message.get("payload")
            usage = payload.get("token_usage") if isinstance(payload, dict) else None
            output = usage.get("output") if isinstance(usage, dict) else None
            if isinstance(output, int):
                output_tokens += output
        metrics = FinalMetrics(
            total_prompt_tokens=None,
            total_completion_tokens=output_tokens or None,
            total_cost_usd=None,
            total_steps=len(steps),
            extra={
                "billing_basis": "subscription",
                "cost_not_reported": True,
                "prompt_tokens_not_reported": True,
                "resume_attempts": self._resume_attempts,
            },
        )
        trajectory = Trajectory(
            schema_version="ATIF-v1.7",
            session_id=session_id or self._session_id or str(uuid.uuid4()),
            agent=Agent(
                name=self.name(),
                version=self._version or "unknown",
                model_name=self.model_name,
                extra={"provider": "kimi-subscription", "oauth": True},
            ),
            steps=steps,
            final_metrics=metrics,
        )
        try:
            (self.logs_dir / "trajectory.json").write_text(
                json.dumps(trajectory.to_json_dict(), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            return
        populate_context_from_final_metrics(context, metrics)


__all__ = ["KIMI_CONFIG", "KimiCode"]
