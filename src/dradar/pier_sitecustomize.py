"""Standalone Pier startup shim copied as ``sitecustomize.py`` per trial.

This module must use only the standard library: it executes inside Pier's own
isolated Python environment, where the ``dradar`` package is not installed.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path


_IMAGE_ENV = "DRADAR_EGRESS_PROXY_IMAGE"
_PATCH_MARKER = "_dradar_prebuilt_egress_v1"
_LOCAL_IMAGE_ID_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_OFFICIAL_DIGEST_PREFIX = (
    "ghcr.io/codex-radar/dradar-egress-proxy@sha256:"
)


def _image_is_immutable(image: str) -> bool:
    return bool(
        _LOCAL_IMAGE_ID_RE.fullmatch(image)
        or (
            image.startswith(_OFFICIAL_DIGEST_PREFIX)
            and _LOCAL_IMAGE_ID_RE.fullmatch(image.split("@", 1)[1])
        )
    )


def _proxy_policy_env(allowlist, token: str) -> dict[str, str]:
    environment = {
        "PROXY_TOKEN": token,
        "ALLOWLIST_DOMAINS": ",".join(allowlist.domains),
    }
    mappings = {
        "DRADAR_EGRESS_UPSTREAM_HOST": "UPSTREAM_PROXY_HOST",
        "DRADAR_EGRESS_UPSTREAM_PORT": "UPSTREAM_PROXY_PORT",
        "DRADAR_EGRESS_UPSTREAM_USERNAME": "UPSTREAM_PROXY_USERNAME",
        "DRADAR_EGRESS_UPSTREAM_PASSWORD": "UPSTREAM_PROXY_PASSWORD",
    }
    for source, target in mappings.items():
        if value := os.environ.get(source):
            environment[target] = value
    return environment


def _write_docker_proxy_compose(
    *, path: Path, proxy_dir: Path, allowlist, token: str,
) -> Path:
    del proxy_dir
    image = os.environ[_IMAGE_ENV]
    proxy_service = {
        "image": image,
        "pull_policy": "never",
        "environment": _proxy_policy_env(allowlist, token),
        "healthcheck": {
            "test": ["CMD-SHELL", "bash -lc '</dev/tcp/127.0.0.1/8080'"],
            "interval": "1s",
            "timeout": "1s",
            "retries": 30,
        },
        "networks": ["pier-egress-internal", "default"],
    }
    if os.environ.get("DRADAR_EGRESS_UPSTREAM_HOST") == "host.docker.internal":
        proxy_service["extra_hosts"] = ["host.docker.internal:host-gateway"]
    compose = {
        "services": {
            "main": {
                "networks": ["pier-egress-internal"],
                "depends_on": {
                    "pier-egress-proxy": {"condition": "service_healthy"},
                },
            },
            "pier-egress-proxy": proxy_service,
        },
        "networks": {"pier-egress-internal": {"internal": True}},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(compose, indent=2), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def _build_proxy_override() -> dict[str, object] | None:
    proxy = os.environ.get("DRADAR_EGRESS_BUILD_PROXY")
    if not proxy:
        return None
    arguments = {
        "HTTP_PROXY": proxy,
        "HTTPS_PROXY": proxy,
        "http_proxy": proxy,
        "https_proxy": proxy,
    }
    if no_proxy := os.environ.get("DRADAR_EGRESS_BUILD_NO_PROXY"):
        arguments.update({"NO_PROXY": no_proxy, "no_proxy": no_proxy})
    build: dict[str, object] = {"args": arguments}
    if os.environ.get("DRADAR_EGRESS_UPSTREAM_HOST") == "host.docker.internal":
        build["extra_hosts"] = ["host.docker.internal=host-gateway"]
    return build


def _finalize_docker_proxy_compose(
    path: Path,
    runtime_environment: dict[str, str],
    build_override: dict[str, object] | None,
) -> None:
    """Move the short-lived proxy token out of `docker compose exec` argv."""

    compose = json.loads(path.read_text(encoding="utf-8"))
    main = compose["services"]["main"]
    environment = dict(main.get("environment") or {})
    environment.update(runtime_environment)
    main["environment"] = environment
    if build_override is not None:
        main["build"] = build_override
    path.write_text(json.dumps(compose, indent=2), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _patch_pier() -> None:
    image = os.environ.get(_IMAGE_ENV)
    if not image:
        return
    if not _image_is_immutable(image):
        raise RuntimeError("DRadar egress image is not pinned by digest")

    from pier.environments import agent_setup
    from pier.environments.docker import docker as docker_environment

    if getattr(docker_environment, _PATCH_MARKER, False):
        return
    agent_setup.write_docker_proxy_compose = _write_docker_proxy_compose
    docker_environment.write_docker_proxy_compose = _write_docker_proxy_compose

    original_prepare = docker_environment.DockerEnvironment._prepare_egress_proxy_compose

    def prepare_with_build_proxy(self) -> None:
        original_prepare(self)
        if self._egress_proxy_compose_path is None:
            return
        path = self._egress_proxy_compose_path
        runtime_environment = dict(self._egress_proxy_env)
        build_override = (
            _build_proxy_override()
            if self.agent_install_spec is not None else None
        )
        _finalize_docker_proxy_compose(
            path, runtime_environment, build_override,
        )
        # The main service already carries these values. Clearing the Pier
        # injection map prevents the short-lived Basic token from appearing in
        # `docker compose exec -e HTTP_PROXY=...` process arguments.
        self._egress_proxy_env = {}

    docker_environment.DockerEnvironment._prepare_egress_proxy_compose = (
        prepare_with_build_proxy
    )
    setattr(docker_environment, _PATCH_MARKER, True)


if __name__ == "sitecustomize":
    try:
        _patch_pier()
    except Exception as exc:  # pragma: no cover - exercised in the Pier subprocess
        # Python normally ignores sitecustomize failures and continues. That would
        # silently fall back to Pier's dynamic apt build, reopening the exact cold
        # machine failure this shim prevents, so fail closed before any task starts.
        sys.stderr.write(
            "DRadar Pier egress bootstrap failed before task start: "
            f"{type(exc).__name__}\n"
        )
        os._exit(78)
