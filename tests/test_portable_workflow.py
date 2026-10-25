import math
from types import SimpleNamespace

import pytest
from click.testing import CliRunner
from pydantic_ai.profiles.openai import OpenAIModelProfile
from pydantic_ai.usage import RunUsage

from backend.agents.swarm import ChallengeSwarm, _model_fallback_spec, _quota_fallback_spec
from backend.cli import main
from backend.config import Settings
from backend.cost_tracker import calc_cost, format_cost
from backend.models import resolve_model, resolve_model_settings
from backend.prompts import ChallengeMeta
from backend.solver_base import FLAG_FOUND, GAVE_UP, QUOTA_ERROR, SolverResult


def test_full_run_requires_explicit_coordinator():
    result = CliRunner().invoke(main, [])
    assert result.exit_code == 2
    assert "--coordinator" in result.output


@pytest.mark.parametrize("slots", ["0", "-1"])
def test_invalid_concurrency_rejected(slots):
    assert CliRunner().invoke(main, ["--challenge", "example", "--max-challenges", slots]).exit_code == 2


def test_magpie_budget_effort_and_tool_compatibility():
    settings = Settings(_env_file=None, magpie_max_tokens=4096)
    model = resolve_model("magpie/group/free-workers/low", settings)
    options = resolve_model_settings("magpie/group/free-workers/low", settings)
    assert model.model_name == "group/free-workers"
    assert options["max_tokens"] == 4096
    assert options["openai_reasoning_effort"] == "low"
    assert not OpenAIModelProfile.from_profile(model.profile).openai_supports_tool_choice_required


def test_unknown_gateway_route_is_not_reported_as_free():
    usage = RunUsage(input_tokens=100, output_tokens=10)
    assert calc_cost(usage, "group/free-workers", "magpie") == 0
    assert math.isnan(calc_cost(usage, "unknown/paid-model", "magpie"))
    assert format_cost(math.nan) == "unknown"


def test_no_automatic_paid_fallback():
    result = SolverResult(None, QUOTA_ERROR, "quota", 1, 0, "")
    assert _quota_fallback_spec("claude-sdk/claude-opus-4-6") is None
    assert _model_fallback_spec("codex/gpt-5.6-sol", result) is None


@pytest.mark.asyncio
async def test_exhausted_solver_stops_without_another_request():
    calls = []

    async def start():
        pass

    async def run():
        calls.append("request")
        return SolverResult(None, GAVE_UP, "tried hypothesis", 1, 0, "")

    solver = SimpleNamespace(start=start, run_until_done_or_gave_up=run)
    swarm = ChallengeSwarm(".", ChallengeMeta(), SimpleNamespace(), SimpleNamespace(),
                           Settings(_env_file=None, solver_max_bumps=0))
    result, _, _ = await swarm._run_solver_loop(solver, "magpie/group/free-workers")
    assert result.status == GAVE_UP
    assert calls == ["request"]


@pytest.mark.parametrize("found,expected", [(False, 2), (True, 0)])
def test_offline_run_does_not_contact_platform_or_clean_other_containers(tmp_path, monkeypatch, found, expected):
    challenge = tmp_path / "fixture"
    challenge.mkdir()
    (challenge / "metadata.yml").write_text("name: fixture\ncategory: crypto\n", encoding="utf-8")
    events = []

    class Client:
        async def get_connection_info(self, name):
            raise AssertionError("Offline challenge must not query platform")

        async def close(self):
            events.append("closed")

    class Swarm:
        def __init__(self, **kwargs):
            assert kwargs["no_submit"] is True

        async def run(self):
            return SolverResult("flag{fixture}", FLAG_FOUND, "", 1, 0, "") if found else None

    async def cleanup():
        raise AssertionError("Must not destroy another process's containers")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("backend.ret2shell.create_competition_client", lambda settings: Client())
    monkeypatch.setattr("backend.agents.swarm.ChallengeSwarm", Swarm)
    monkeypatch.setattr("backend.sandbox.cleanup_orphan_containers", cleanup)
    result = CliRunner().invoke(main, ["--challenge", str(challenge), "--no-submit"])
    assert result.exit_code == expected, result.output
    assert events == ["closed"]
    if found:
        assert "FLAG CANDIDATE (not submitted)" in result.output
