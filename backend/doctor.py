"""Preflight doctor for the Magpie CTF workflow.

Offline checks are the default. Network and Docker daemon checks run only when
``--live`` is supplied. The doctor never sends inference requests, edits
configuration, installs software, or prints credentials or response bodies.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable
from typing import Any
from urllib.parse import urlsplit

import click
import httpx

from backend.config import Settings
from backend.models import DEFAULT_MODELS, model_id_from_spec, provider_from_spec

REQUEST_TIMEOUT_SECONDS = 10.0
DOCKER_TIMEOUT_SECONDS = 10.0

DockerRunner = Callable[..., subprocess.CompletedProcess[str]]


class DoctorError(Exception):
    """A check failure whose message is already safe to display."""


def _check(
    name: str,
    status: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {"name": name, "status": status, "message": message, "details": details or {}}


def _python_check() -> dict[str, Any]:
    version = sys.version_info
    text = f"{version[0]}.{version[1]}.{version[2]}"
    if (version[0], version[1]) >= (3, 14):
        return _check("python", "ok", f"Python {text} satisfies >= 3.14.")
    return _check("python", "fail", f"Python {text} is too old; Python >= 3.14 is required.")


def _validate_base_url(raw: str) -> tuple[bool, str]:
    try:
        parsed = urlsplit(raw)
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError:
        return False, "MAGPIE_BASE_URL is not a valid URL."

    if parsed.scheme not in {"http", "https"}:
        return False, "MAGPIE_BASE_URL must use http or https."
    if not hostname:
        return False, "MAGPIE_BASE_URL must include a host."
    if parsed.username or parsed.password:
        return False, "MAGPIE_BASE_URL must not include credentials."
    if parsed.query or parsed.fragment:
        return False, "MAGPIE_BASE_URL must not include a query or fragment."
    if not parsed.path.rstrip("/").endswith("/v1"):
        return False, "MAGPIE_BASE_URL path must end with /v1."
    return True, "MAGPIE_BASE_URL uses http/https and its path ends with /v1."


def _parse_model_specs(specs: list[str]) -> tuple[list[str], list[str]]:
    model_ids: list[str] = []
    errors: list[str] = []
    if not specs:
        errors.append("At least one model spec is required.")

    for raw in specs:
        spec = raw.strip()
        if not spec or "/" not in spec:
            errors.append(f"Invalid model spec: {raw!r}.")
            continue

        provider = provider_from_spec(spec)
        if provider != "magpie":
            errors.append(f"Model spec {raw!r} is not a magpie/* spec.")
            continue

        model_id = model_id_from_spec(spec)
        invalid_model_id = (
            not model_id
            or not model_id.strip("/")
            or "//" in model_id
            or model_id.startswith("/")
            or model_id.endswith("/")
        )
        if invalid_model_id:
            errors.append(f"Model spec {raw!r} has an invalid model ID.")
            continue
        if model_id not in model_ids:
            model_ids.append(model_id)

    return model_ids, errors


def _make_http_client(
    *,
    timeout: float,
    transport: httpx.BaseTransport | None = None,
) -> httpx.Client:
    return httpx.Client(timeout=timeout, transport=transport, follow_redirects=False)


def _fetch_model_ids(
    settings: Settings,
    *,
    transport: httpx.BaseTransport | None = None,
) -> list[str]:
    url = f"{settings.magpie_base_url.rstrip('/')}/models"
    headers = {"Accept": "application/json"}
    if settings.magpie_api_key:
        headers["Authorization"] = f"Bearer {settings.magpie_api_key}"

    try:
        with _make_http_client(timeout=REQUEST_TIMEOUT_SECONDS, transport=transport) as client:
            response = client.get(url, headers=headers)
    except httpx.TimeoutException:
        raise DoctorError(
            f"Magpie /models request timed out after {REQUEST_TIMEOUT_SECONDS:g} seconds."
        ) from None
    except httpx.HTTPError as exc:
        raise DoctorError(f"Magpie /models request failed ({type(exc).__name__}).") from None
    except Exception as exc:
        raise DoctorError(f"Magpie /models request failed ({type(exc).__name__}).") from None

    if not 200 <= response.status_code < 300:
        raise DoctorError(f"Magpie /models returned HTTP {response.status_code}.")

    try:
        payload = response.json()
    except ValueError:
        raise DoctorError("Magpie /models returned invalid JSON.") from None

    data = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(data, list):
        raise DoctorError("Magpie /models response did not contain a model list.")

    model_ids: list[str] = []
    for item in data:
        model_id = item.get("id") if isinstance(item, dict) else item
        if isinstance(model_id, str) and model_id:
            model_ids.append(model_id)
    return model_ids


def _magpie_live_check(
    settings: Settings,
    model_ids: list[str],
    transport: httpx.BaseTransport | None,
) -> dict[str, Any]:
    try:
        available = _fetch_model_ids(settings, transport=transport)
    except DoctorError as exc:
        return _check("magpie_live", "fail", str(exc), {"requested": model_ids})
    except Exception as exc:
        return _check(
            "magpie_live",
            "fail",
            f"Magpie /models check failed ({type(exc).__name__}).",
            {"requested": model_ids},
        )

    available_set = set(available)
    missing = [model_id for model_id in model_ids if model_id not in available_set]
    if missing:
        return _check(
            "magpie_live",
            "fail",
            "Magpie /models is reachable but requested model(s) are missing: "
            + ", ".join(missing)
            + ".",
            {
                "requested": model_ids,
                "missing": missing,
                "available_count": len(available),
            },
        )

    return _check(
        "magpie_live",
        "ok",
        "Magpie /models is reachable; requested model(s) are present: "
        + ", ".join(model_ids)
        + ".",
        {"requested": model_ids, "available_count": len(available)},
    )


def _run_docker(
    executable: str,
    args: list[str],
    *,
    timeout: float = DOCKER_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [executable, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        stdin=subprocess.DEVNULL,
    )


def _docker_live_checks(
    docker_path: str | None,
    image: str | None,
    runner: DockerRunner,
) -> list[dict[str, Any]]:
    if docker_path is None:
        return [
            _check("docker_info", "fail", "Docker executable not found; docker info was not run."),
            _check("docker_image", "skip", "Skipped because Docker executable is unavailable."),
        ]

    try:
        info = runner(docker_path, ["info"], timeout=DOCKER_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return [
            _check(
                "docker_info",
                "fail",
                f"docker info timed out after {DOCKER_TIMEOUT_SECONDS:g} seconds.",
            ),
            _check("docker_image", "skip", "Skipped because docker info did not complete."),
        ]
    except Exception as exc:
        return [
            _check("docker_info", "fail", f"docker info failed ({type(exc).__name__})."),
            _check("docker_image", "skip", "Skipped because docker info did not complete."),
        ]

    if info.returncode != 0:
        return [
            _check("docker_info", "fail", f"docker info failed (exit code {info.returncode})."),
            _check("docker_image", "skip", "Skipped because docker info failed."),
        ]

    checks = [_check("docker_info", "ok", "Docker daemon responded to docker info.")]
    if image is None:
        checks.append(_check("docker_image", "skip", "Skipped because settings failed to load."))
        return checks

    try:
        inspect = runner(
            docker_path,
            ["image", "inspect", image],
            timeout=DOCKER_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        checks.append(
            _check(
                "docker_image",
                "fail",
                f"docker image inspect timed out after {DOCKER_TIMEOUT_SECONDS:g} seconds.",
            )
        )
        return checks
    except Exception as exc:
        checks.append(
            _check("docker_image", "fail", f"docker image inspect failed ({type(exc).__name__}).")
        )
        return checks

    if inspect.returncode != 0:
        checks.append(
            _check(
                "docker_image",
                "fail",
                f"Docker image '{image}' is not available locally "
                f"(docker image inspect exit code {inspect.returncode}).",
            )
        )
    else:
        checks.append(_check("docker_image", "ok", f"Docker image '{image}' is available locally."))
    return checks


def _build_report(checks: list[dict[str, Any]], *, live: bool) -> dict[str, Any]:
    failures = sum(1 for check in checks if check["status"] == "fail")
    ok = failures == 0

    if live:
        summary = (
            "Live preflight passed; Magpie /models and Docker checks completed."
            if ok
            else f"Live preflight failed: {failures} check(s) failed."
        )
    elif ok:
        summary = (
            "Offline preflight passed; Magpie /models was not requested and no Docker "
            "daemon/image check was performed. Network and model availability are unverified."
        )
    else:
        summary = (
            f"Offline preflight failed: {failures} check(s) failed. Magpie /models was not "
            "requested and no Docker daemon/image check was performed."
        )

    return {
        "ok": ok,
        "live": live,
        "mode": "live" if live else "offline",
        "summary": summary,
        "checks": checks,
    }


def run_doctor(
    *,
    live: bool,
    model_specs: Iterable[str] | None = None,
    settings: Settings | None = None,
    transport: httpx.BaseTransport | None = None,
    docker_runner: DockerRunner | None = None,
) -> dict[str, Any]:
    """Run preflight checks and return a JSON-serializable report."""
    checks: list[dict[str, Any]] = [_python_check()]

    loaded = settings
    if loaded is None:
        try:
            loaded = Settings()
        except Exception as exc:
            checks.append(
                _check(
                    "settings",
                    "fail",
                    f"Settings() failed to load ({type(exc).__name__}); check .env values.",
                )
            )
        else:
            checks.append(_check("settings", "ok", "Settings loaded with default .env lookup."))
    else:
        checks.append(_check("settings", "ok", "Settings loaded for preflight."))

    base_url_ok = False
    if loaded is None:
        checks.append(
            _check("magpie_base_url", "skip", "Not checked because settings failed to load.")
        )
    else:
        base_url_ok, base_url_message = _validate_base_url(loaded.magpie_base_url)
        checks.append(
            _check(
                "magpie_base_url",
                "ok" if base_url_ok else "fail",
                base_url_message,
            )
        )

    specs = list(DEFAULT_MODELS) if model_specs is None else list(model_specs)
    model_ids, model_errors = _parse_model_specs(specs)
    model_source = "default free pool" if model_specs is None else "cli"
    if model_errors:
        checks.append(
            _check(
                "magpie_models",
                "fail",
                "Model spec validation failed: " + " ".join(model_errors),
                {"source": model_source, "requested": specs},
            )
        )
    else:
        checks.append(
            _check(
                "magpie_models",
                "ok",
                "Magpie model specs parsed.",
                {
                    "source": model_source,
                    "requested": specs,
                    "model_ids": model_ids,
                },
            )
        )

    docker_path = shutil.which("docker")
    if docker_path:
        checks.append(
            _check(
                "docker_executable",
                "ok",
                "Docker executable found (optional in offline mode).",
                {"path": docker_path},
            )
        )
    else:
        checks.append(
            _check(
                "docker_executable",
                "warn",
                "Docker executable not found (optional in offline mode; required for --live).",
            )
        )

    runner = docker_runner or _run_docker
    if live:
        if loaded is None or not base_url_ok:
            checks.append(
                _check(
                    "magpie_live",
                    "skip",
                    "Skipped because settings or MAGPIE_BASE_URL are invalid.",
                )
            )
        elif model_errors:
            checks.append(_check("magpie_live", "skip", "Skipped because model specs are invalid."))
        else:
            checks.append(_magpie_live_check(loaded, model_ids, transport))

        image = loaded.sandbox_image if loaded is not None else None
        checks.extend(_docker_live_checks(docker_path, image, runner))
    else:
        checks.append(
            _check(
                "magpie_live",
                "skip",
                "Skipped: offline mode; Magpie /models was not requested and network "
                "availability is unverified.",
            )
        )
        checks.append(
            _check(
                "docker_live",
                "skip",
                "Skipped: offline mode; Docker daemon and sandbox image were not checked.",
            )
        )

    return _build_report(checks, live=live)


def _unexpected_report(*, live: bool, error_name: str) -> dict[str, Any]:
    report = _build_report(
        [_check("doctor", "fail", f"Preflight failed unexpectedly ({error_name}).")],
        live=live,
    )
    report["summary"] = "Preflight failed unexpectedly; no configuration was changed."
    return report


def _print_human(report: dict[str, Any]) -> None:
    mode = "live" if report["live"] else "offline"
    click.echo(f"Magpie preflight ({mode})")
    for check in report["checks"]:
        click.echo(f"[{check['status']}] {check['name']}: {check['message']}")
    click.echo(report["summary"])


@click.command()
@click.option("--live", is_flag=True, help="Run Magpie /models and Docker daemon/image checks.")
@click.option("--json", "json_output", is_flag=True, help="Write one JSON object to stdout.")
@click.option(
    "--model",
    "models",
    multiple=True,
    help="Magpie model spec (repeatable). Defaults to the free-worker pool.",
)
def main(live: bool, json_output: bool, models: tuple[str, ...]) -> None:
    """Preflight the Magpie workflow without changing configuration."""
    try:
        report = run_doctor(live=live, model_specs=list(models) if models else None)
    except Exception as exc:
        report = _unexpected_report(live=live, error_name=type(exc).__name__)

    if json_output:
        click.echo(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        _print_human(report)

    if not report["ok"]:
        raise click.exceptions.Exit(1)


if __name__ == "__main__":
    main()
