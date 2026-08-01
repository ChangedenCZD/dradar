"""Per-machine guardrails: single-instance lock + orphan compose sweep."""
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import dradar.machine as machine
from dradar.machine import acquire_run_lock, sweep_orphan_compose


def test_second_instance_refuses_to_start(tmp_path: Path):
    # A real second PROCESS must be refused (flock allows re-locking within
    # one process, so an in-process double-acquire proves nothing).
    holder = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(f"""
            import sys, time
            sys.path.insert(0, {str(Path(__file__).parent.parent / 'src')!r})
            from pathlib import Path
            from dradar.machine import acquire_run_lock
            acquire_run_lock(Path({str(tmp_path)!r}))
            print("locked", flush=True)
            time.sleep(30)
        """)],
        stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "locked"
        with pytest.raises(SystemExit) as exc:
            acquire_run_lock(tmp_path)
        assert "another dradar run" in str(exc.value)
        assert "PID" in str(exc.value)
    finally:
        holder.kill()
        holder.wait()
    # holder dead -> the lock died with it, no stale-lock cleanup needed
    acquire_run_lock(tmp_path)
    machine._lock_handle.close()
    machine._lock_handle = None


def _fake_compose(monkeypatch, projects, home: Path, owned=()):
    calls = []
    owned = set(owned)
    id_to_project = {f"container-{i}": project
                     for i, project in enumerate(projects)}
    project_to_id = {project: container_id
                     for container_id, project in id_to_project.items()}

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if cmd[:3] == ["docker", "compose", "ls"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps([{"Name": p} for p in projects]), stderr="")
        if cmd[:2] == ["docker", "ps"]:
            project = cmd[-1].rsplit("=", 1)[-1]
            container_id = project_to_id.get(project)
            stdout = f"{container_id}\n" if container_id else ""
            return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")
        if cmd[:2] == ["docker", "inspect"]:
            inspected = []
            for container_id in cmd[2:]:
                project = id_to_project[container_id]
                root = home if project in owned else home.parent / "other-home"
                inspected.append({
                    "Mounts": [{
                        "Type": "bind",
                        "Source": str(root / "work" / "jobs" / project / "artifacts"),
                    }],
                    "Config": {"Labels": {}},
                })
            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps(inspected), stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(machine.subprocess, "run", fake_run)
    return calls


def test_sweep_downs_only_owned_pier_shaped_projects(monkeypatch, capsys,
                                                      tmp_path: Path):
    first = "arktype-json-schema-refs-depende__ui7n6a5"
    second = "boa-hierarchical-evaluation-canc__eeecwyc"
    calls = _fake_compose(monkeypatch, [
        first,                                           # ours
        second,                                          # foreign runner
        "my-blog",                                     # someone's real project
        "web_app-dev",                                 # underscore but not __id
    ], tmp_path, owned={first})
    sweep_orphan_compose(tmp_path, assume_yes=True)
    downed = [c[3] for c in calls if c[:3] == ["docker", "compose", "-p"]]
    assert downed == [first]
    out = capsys.readouterr().out
    assert "burning your quota" in out


def test_sweep_silent_when_nothing_matches(monkeypatch, capsys, tmp_path: Path):
    calls = _fake_compose(monkeypatch, ["my-blog"], tmp_path)
    sweep_orphan_compose(tmp_path, assume_yes=True)
    assert len(calls) == 1                       # only the ls, no downs
    assert capsys.readouterr().out == ""


def test_sweep_survives_missing_docker(monkeypatch, tmp_path: Path):
    def boom(cmd, **kw):
        raise OSError("no docker")
    monkeypatch.setattr(machine.subprocess, "run", boom)
    sweep_orphan_compose(tmp_path, assume_yes=True)  # must not raise


def test_sweep_asks_before_touching_anything(monkeypatch, capsys,
                                             tmp_path: Path):
    project = "some-task__abc1234"
    calls = _fake_compose(monkeypatch, [project], tmp_path, owned={project})
    monkeypatch.setattr("builtins.input", lambda *_: "n")
    sweep_orphan_compose(tmp_path, assume_yes=False)
    assert not any(c[:3] == ["docker", "compose", "-p"] for c in calls)
    assert "docker compose -p" in capsys.readouterr().out


def test_sweep_fails_closed_when_inspect_fails(monkeypatch, capsys,
                                                tmp_path: Path):
    project = "some-task__abc1234"
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if cmd[:3] == ["docker", "compose", "ls"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps([{"Name": project}]), stderr="")
        if cmd[:2] == ["docker", "ps"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="container-1\n", stderr="")
        if cmd[:2] == ["docker", "inspect"]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(machine.subprocess, "run", fake_run)
    sweep_orphan_compose(tmp_path, assume_yes=True)
    assert not any(c[:3] == ["docker", "compose", "-p"] for c in calls)
    assert capsys.readouterr().out == ""
