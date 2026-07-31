"""DeepSeek V4 Flash is an additive, public-safe Codex provider."""

import os
import tomllib
from pathlib import Path

import pytest

import dradar.runner as runner
from dradar.providers import (
    DEFAULT_CODEX_PROVIDER,
    DEEPSEEK_API_KEY_ENV,
    DEEPSEEK_CAPABILITY,
    DEEPSEEK_CODEX_VERSION,
    DEEPSEEK_MODEL,
    DEEPSEEK_PROVIDER,
    advertised_capabilities,
    assignment_codex_provider,
)
from dradar.runner import RunnerError


def _assignment(**overrides) -> dict:
    values = {
        "assignment_id": "a1",
        "task_id": "task-1",
        "agent": "codex",
        "provider": DEEPSEEK_PROVIDER,
        "model": DEEPSEEK_MODEL,
        "effort": "max",
        "agent_version": DEEPSEEK_CODEX_VERSION,
        "resume_generation": 0,
        "est_minutes": 5,
    }
    values.update(overrides)
    return values


def _command(tmp_path: Path, monkeypatch, assignment=None) -> tuple[list[str], Path]:
    assignment = assignment or _assignment()
    monkeypatch.setattr(runner.shutil, "which", lambda name: "/usr/bin/pier")
    tasks = tmp_path / "tasks"
    (tasks / assignment["task_id"]).mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir()
    auth = tmp_path / "runtime-auth.json"
    auth.write_text("{}")
    if os.name != "nt":
        auth.chmod(0o600)
    return (
        runner.build_pier_command(
            assignment,
            tasks,
            tmp_path / "jobs",
            "job",
            home,
            provider_auth_path=auth,
        ),
        home,
    )


def test_capability_advertises_software_support_before_first_key_setup():
    assert advertised_capabilities({}) == (DEEPSEEK_CAPABILITY,)
    assert advertised_capabilities({DEEPSEEK_API_KEY_ENV: "key"}) == (
        DEEPSEEK_CAPABILITY,
    )


def test_missing_provider_preserves_original_codex_path():
    assert assignment_codex_provider({"agent": "codex"}) == DEFAULT_CODEX_PROVIDER
    assert assignment_codex_provider({"agent": "claude-code"}) is None


def test_command_uses_stock_codex_and_auth_file_without_secret_env(
    tmp_path: Path,
    monkeypatch,
):
    command, home = _command(tmp_path, monkeypatch)
    joined = " ".join(command)

    assert command[:5] == [
        "/usr/bin/pier", "--isolated", "--from",
        "datacurve-pier==0.3.0", "pier",
    ]
    assert command[command.index("--agent") + 1] == "codex"
    assert "--agent-import-path" not in command
    assert runner.PIER_SPEC not in joined
    assert f"version={DEEPSEEK_CODEX_VERSION}" in command
    assert "reasoning_effort=max" in command
    assert "checkpoint_enabled=true" not in command
    assert "checkpoint_path=" not in joined
    assert DEEPSEEK_API_KEY_ENV not in joined
    assert "CODEX_AUTH_JSON_PATH=" in joined

    config_arg = next(
        item for item in command if item.startswith("config_toml_file=")
    )
    config_path = Path(config_arg.split("=", 1)[1])
    assert config_path == home / "codex-deepseek-v4-flash.toml"
    parsed = tomllib.loads(config_path.read_text())
    assert parsed["model_provider"] == DEEPSEEK_PROVIDER
    assert parsed["model_context_window"] == 1_048_576
    assert "model_catalog_json" not in parsed
    assert parsed["model_providers"][DEEPSEEK_PROVIDER] == {
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com/",
        "wire_api": "responses",
        "requires_openai_auth": True,
    }


@pytest.mark.parametrize("effort", ["low", "medium", "xhigh"])
def test_compatibility_aliases_are_not_duplicate_benchmark_cells(
    tmp_path: Path,
    monkeypatch,
    effort: str,
):
    with pytest.raises(RunnerError, match="effort must be one of high, max"):
        _command(tmp_path, monkeypatch, _assignment(effort=effort))


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"model": "deepseek-other"}, "unsupported DeepSeek model"),
        ({"effort": "ultra"}, "effort must be"),
        ({"agent_version": "0.145.0"}, "pinned to tested Codex"),
        ({"agent_version": "latest"}, "exact stable"),
    ],
)
def test_rejects_unverified_assignment(
    tmp_path: Path,
    monkeypatch,
    overrides,
    message,
):
    with pytest.raises(RunnerError, match=message):
        _command(tmp_path, monkeypatch, _assignment(**overrides))


def test_unknown_provider_fails_without_touching_openai_auth(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setattr(runner.shutil, "which", lambda name: "/usr/bin/pier")
    tasks = tmp_path / "tasks"
    (tasks / "task-1").mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir()
    with pytest.raises(RunnerError, match="unsupported Codex provider"):
        runner.build_pier_command(
            _assignment(provider="future-provider"),
            tasks,
            tmp_path / "jobs",
            "job",
            home,
        )


def test_missing_runtime_auth_file_is_rejected(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(runner.shutil, "which", lambda name: "/usr/bin/pier")
    tasks = tmp_path / "tasks"
    (tasks / "task-1").mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir()
    with pytest.raises(RunnerError, match="runtime credential"):
        runner.build_pier_command(
            _assignment(), tasks, tmp_path / "jobs", "job", home,
        )


def test_deepseek_checkpoint_resume_is_rejected(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(runner.shutil, "which", lambda name: "/usr/bin/pier")
    tasks = tmp_path / "tasks"
    (tasks / "task-1").mkdir(parents=True)
    auth = tmp_path / "auth.json"
    auth.write_text("{}")
    with pytest.raises(RunnerError, match="checkpoints are not supported"):
        runner.build_pier_command(
            _assignment(), tasks, tmp_path / "jobs", "job", tmp_path,
            resume_checkpoint=tmp_path / "checkpoint",
            provider_auth_path=auth,
        )


def test_pier_process_drops_ambient_deepseek_key(monkeypatch):
    monkeypatch.setenv(DEEPSEEK_API_KEY_ENV, "sentinel-deepseek-secret")

    deepseek_env = runner._pier_process_env(_assignment())
    openai_env = runner._pier_process_env(
        _assignment(provider=DEFAULT_CODEX_PROVIDER, model="gpt-5.5")
    )

    assert DEEPSEEK_API_KEY_ENV not in deepseek_env
    assert openai_env[DEEPSEEK_API_KEY_ENV] == "sentinel-deepseek-secret"


def test_run_removes_temporary_auth_when_command_build_fails(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setenv(DEEPSEEK_API_KEY_ENV, "sentinel-deepseek-secret")
    created = []
    original = runner.create_deepseek_auth_json

    def create(directory):
        path = original(directory)
        created.append(path)
        return path

    monkeypatch.setattr(runner, "create_deepseek_auth_json", create)
    monkeypatch.setattr(
        runner,
        "build_pier_command",
        lambda *args, **kwargs: (_ for _ in ()).throw(RunnerError("intentional")),
    )

    with pytest.raises(RunnerError, match="intentional"):
        runner.run_trial(_assignment(), tmp_path / "tasks", tmp_path / "work")

    assert len(created) == 1
    assert not created[0].exists()


def test_deepseek_run_never_queries_npm_latest(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(DEEPSEEK_API_KEY_ENV, "sentinel-deepseek-secret")
    monkeypatch.setattr(
        runner,
        "resolve_latest_codex_cli_version",
        lambda *args, **kwargs: pytest.fail("DeepSeek must use the tested fixed pin"),
    )
    seen = {}

    def stop_after_version(assignment, *args, **kwargs):
        seen["version"] = assignment["agent_version"]
        raise RunnerError("intentional test stop")

    monkeypatch.setattr(runner, "build_pier_command", stop_after_version)
    with pytest.raises(RunnerError, match="intentional test stop"):
        runner.run_trial(_assignment(), tmp_path / "tasks", tmp_path / "work")
    assert seen["version"] == DEEPSEEK_CODEX_VERSION
