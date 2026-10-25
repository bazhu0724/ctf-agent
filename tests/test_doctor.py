from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from click.testing import CliRunner

from backend import doctor
from backend.config import Settings

SECRET_KEY = "super-secret-magpie-key"


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "magpie_base_url": "http://127.0.0.1:3425/v1",
        "magpie_api_key": SECRET_KEY,
        "sandbox_image": "ctf-sandbox",
    }
    values.update(overrides)
    return Settings(**values)


def _models_response(ids: list[str]) -> httpx.Response:
    return httpx.Response(
        200,
        json={"object": "list", "data": [{"id": model_id, "object": "model"} for model_id in ids]},
    )


class FakeDocker:
    def __init__(self, *, info_code: int = 0, image_code: int = 0) -> None:
        self.info_code = info_code
        self.image_code = image_code
        self.calls: list[list[str]] = []

    def __call__(
        self, executable: str, args: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append([executable, *args])
        code = self.info_code if args == ["info"] else self.image_code
        return subprocess.CompletedProcess([executable, *args], code, "", "")


def _find(report: dict[str, Any], name: str) -> dict[str, Any]:
    return next(check for check in report["checks"] if check["name"] == name)


def test_offline_success_reports_unverified_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: "C:/fake/docker.exe")

    def forbidden_request(*_args: Any, **_kwargs: Any) -> httpx.Response:
        raise AssertionError("offline mode must not perform HTTP requests")

    report = doctor.run_doctor(
        live=False,
        settings=_settings(),
        transport=httpx.MockTransport(forbidden_request),
        docker_runner=FakeDocker(),
    )

    assert report["ok"] is True
    assert report["live"] is False
    assert report["mode"] == "offline"
    assert "not requested" in report["summary"]
    assert "unverified" in report["summary"]
    assert _find(report, "magpie_models")["details"]["model_ids"] == ["group/free-workers"]
    assert _find(report, "magpie_models")["details"]["source"] == "default free pool"
    assert _find(report, "magpie_live")["status"] == "skip"
    assert _find(report, "docker_live")["status"] == "skip"


def test_live_success_checks_models_and_docker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: "C:/fake/docker.exe")
    docker = FakeDocker()
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _models_response(["group/free-workers", "group/other"])

    report = doctor.run_doctor(
        live=True,
        settings=_settings(),
        transport=httpx.MockTransport(handler),
        docker_runner=docker,
    )

    assert report["ok"] is True
    assert report["mode"] == "live"
    assert len(seen) == 1
    assert str(seen[0].url) == "http://127.0.0.1:3425/v1/models"
    assert seen[0].headers["Authorization"] == f"Bearer {SECRET_KEY}"
    assert _find(report, "magpie_live")["status"] == "ok"
    assert _find(report, "docker_info")["status"] == "ok"
    assert _find(report, "docker_image")["status"] == "ok"
    assert docker.calls == [
        ["C:/fake/docker.exe", "info"],
        ["C:/fake/docker.exe", "image", "inspect", "ctf-sandbox"],
    ]


def test_live_missing_model_fails_without_leaking_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: "C:/fake/docker.exe")

    def handler(_request: httpx.Request) -> httpx.Response:
        return _models_response(["group/other"])

    report = doctor.run_doctor(
        live=True,
        settings=_settings(),
        transport=httpx.MockTransport(handler),
        docker_runner=FakeDocker(),
    )

    assert report["ok"] is False
    live_check = _find(report, "magpie_live")
    assert live_check["status"] == "fail"
    assert live_check["details"]["missing"] == ["group/free-workers"]
    assert SECRET_KEY not in json.dumps(report)


def test_live_network_error_does_not_leak_key_or_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: "C:/fake/docker.exe")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"boom {SECRET_KEY} {request.url}", request=request)

    report = doctor.run_doctor(
        live=True,
        settings=_settings(),
        transport=httpx.MockTransport(handler),
        docker_runner=FakeDocker(),
    )

    assert report["ok"] is False
    live_check = _find(report, "magpie_live")
    assert live_check["status"] == "fail"
    assert "ConnectError" in live_check["message"]
    serialized = json.dumps(report)
    assert SECRET_KEY not in serialized
    assert "boom" not in serialized


def test_live_http_error_body_is_not_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: "C:/fake/docker.exe")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text=f"internal failure with {SECRET_KEY}")

    report = doctor.run_doctor(
        live=True,
        settings=_settings(),
        transport=httpx.MockTransport(handler),
        docker_runner=FakeDocker(),
    )

    live_check = _find(report, "magpie_live")
    assert live_check["status"] == "fail"
    assert live_check["message"] == "Magpie /models returned HTTP 500."
    assert SECRET_KEY not in json.dumps(report)


@pytest.mark.parametrize(
    "base_url",
    [
        "ftp://127.0.0.1:3425/v1",
        "http://127.0.0.1:3425",
        "http://127.0.0.1:3425/v2",
        "not-a-url",
    ],
)
def test_invalid_base_url_fails_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, base_url: str
) -> None:
    monkeypatch.chdir(tmp_path)

    report = doctor.run_doctor(live=False, settings=_settings(magpie_base_url=base_url))

    assert report["ok"] is False
    assert _find(report, "magpie_base_url")["status"] == "fail"


def test_custom_model_option_is_parsed_and_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: "C:/fake/docker.exe")

    def handler(_request: httpx.Request) -> httpx.Response:
        return _models_response(["group/paid"])

    report = doctor.run_doctor(
        live=True,
        model_specs=["magpie/group/paid/high"],
        settings=_settings(),
        transport=httpx.MockTransport(handler),
        docker_runner=FakeDocker(),
    )

    models = _find(report, "magpie_models")
    assert models["details"]["model_ids"] == ["group/paid"]
    assert models["details"]["source"] == "cli"
    assert _find(report, "magpie_live")["status"] == "ok"


def test_non_magpie_spec_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)

    report = doctor.run_doctor(live=False, model_specs=["openai/gpt-5"], settings=_settings())

    assert report["ok"] is False
    assert "not a magpie" in _find(report, "magpie_models")["message"]


def test_live_docker_info_failure_skips_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: "C:/fake/docker.exe")
    docker = FakeDocker(info_code=1)

    def handler(_request: httpx.Request) -> httpx.Response:
        return _models_response(["group/free-workers"])

    report = doctor.run_doctor(
        live=True,
        settings=_settings(),
        transport=httpx.MockTransport(handler),
        docker_runner=docker,
    )

    assert report["ok"] is False
    assert _find(report, "docker_info")["status"] == "fail"
    assert _find(report, "docker_image")["status"] == "skip"
    assert docker.calls == [["C:/fake/docker.exe", "info"]]


def test_live_missing_docker_executable_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: None)

    def handler(_request: httpx.Request) -> httpx.Response:
        return _models_response(["group/free-workers"])

    report = doctor.run_doctor(
        live=True,
        settings=_settings(),
        transport=httpx.MockTransport(handler),
        docker_runner=FakeDocker(),
    )

    assert report["ok"] is False
    assert _find(report, "docker_executable")["status"] == "warn"
    assert _find(report, "docker_info")["status"] == "fail"


def test_cli_json_is_single_object_and_nonzero_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    fake_report = doctor._build_report(
        [doctor._check("python", "fail", "fixture failure")],
        live=False,
    )
    monkeypatch.setattr(doctor, "run_doctor", lambda **_kwargs: fake_report)

    result = CliRunner().invoke(doctor.main, ["--json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["checks"][0]["name"] == "python"
    assert result.stdout.strip().startswith("{")
    assert result.stdout.strip().endswith("}")


def test_cli_offline_success_exit_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: "C:/fake/docker.exe")
    monkeypatch.delenv("MAGPIE_BASE_URL", raising=False)
    monkeypatch.delenv("MAGPIE_API_KEY", raising=False)

    result = CliRunner().invoke(doctor.main, ["--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["mode"] == "offline"
    assert payload["ok"] is True
    assert "not requested" in payload["summary"]


def test_module_entrypoint_runs() -> None:
    project_root = Path(__file__).resolve().parents[1]
    # The source venv has an editable install pointing at the source checkout, so
    # force this subprocess to resolve `backend` from the output repo instead.
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([str(project_root), *filter(None, [env.get("PYTHONPATH")])])
    env.pop("PYTHONHOME", None)

    result = subprocess.run(
        [sys.executable, "-m", "backend.doctor", "--json"],
        cwd=project_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["live"] is False
    assert SECRET_KEY not in result.stdout

    module_path = subprocess.run(
        [sys.executable, "-c", "import backend.doctor as d; print(d.__file__)"],
        cwd=project_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
        env=env,
    )
    assert module_path.returncode == 0, module_path.stderr
    assert (
        Path(module_path.stdout.strip()).resolve()
        == (project_root / "backend" / "doctor.py").resolve()
    )
