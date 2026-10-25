from pathlib import Path
from types import SimpleNamespace

from backend.agents.coordinator_core import (
    can_start_without_endpoint,
    select_model_specs_for_challenge,
)
from backend.prompts import ChallengeMeta


def _deps(split=True):
    return SimpleNamespace(
        model_specs=["magpie/group/free-workers", "magpie/group/auto-beam-free"],
        split_models_across_challenges=split,
        challenge_model_assignments={},
        model_assignment_cursor=0,
    )


def test_split_model_assignment_round_robins_challenges():
    deps = _deps()

    assert select_model_specs_for_challenge(deps, "web-a") == ["magpie/group/free-workers"]
    assert select_model_specs_for_challenge(deps, "crypto-b") == ["magpie/group/auto-beam-free"]
    assert select_model_specs_for_challenge(deps, "misc-c") == ["magpie/group/free-workers"]


def test_split_model_assignment_is_sticky_for_challenge():
    deps = _deps()

    first = select_model_specs_for_challenge(deps, "reverse-a")
    second = select_model_specs_for_challenge(deps, "reverse-a")

    assert first == second == ["magpie/group/free-workers"]
    assert deps.model_assignment_cursor == 1


def test_race_model_assignment_keeps_all_models():
    deps = _deps(split=False)

    assert select_model_specs_for_challenge(deps, "pwn-a") == [
        "magpie/group/free-workers",
        "magpie/group/auto-beam-free",
    ]
    assert deps.model_assignment_cursor == 0


def test_split_model_assignment_uses_category_when_loads_are_tied():
    deps = _deps()

    assert select_model_specs_for_challenge(deps, "rsa-a", "Crypto") == ["magpie/group/auto-beam-free"]


def test_split_model_assignment_balances_active_model_loads():
    deps = _deps()
    deps.swarms = {
        "active-crypto": SimpleNamespace(model_specs=["magpie/group/auto-beam-free"]),
    }

    assert select_model_specs_for_challenge(deps, "rsa-b", "Crypto") == ["magpie/group/free-workers"]


def test_offline_friendly_challenges_can_start_without_endpoint(tmp_path: Path):
    dist = tmp_path / "distfiles"
    dist.mkdir()
    (dist / "chall.py").write_text("print('hello')", encoding="utf-8")

    meta = ChallengeMeta(name="local", category="Web", description="", connection_info="")

    assert can_start_without_endpoint(meta, str(tmp_path)) is True


def test_remote_only_web_challenge_waits_for_endpoint(tmp_path: Path):
    meta = ChallengeMeta(name="remote", category="Web", description="", connection_info="")

    assert can_start_without_endpoint(meta, str(tmp_path)) is False
