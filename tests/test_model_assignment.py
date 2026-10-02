from types import SimpleNamespace

from backend.agents.coordinator_core import select_model_specs_for_challenge


def _deps(split=True):
    return SimpleNamespace(
        model_specs=["codex/gpt-5.5", "deepseek/deepseek-flash"],
        split_models_across_challenges=split,
        challenge_model_assignments={},
        model_assignment_cursor=0,
    )


def test_split_model_assignment_round_robins_challenges():
    deps = _deps()

    assert select_model_specs_for_challenge(deps, "web-a") == ["codex/gpt-5.5"]
    assert select_model_specs_for_challenge(deps, "crypto-b") == ["deepseek/deepseek-flash"]
    assert select_model_specs_for_challenge(deps, "misc-c") == ["codex/gpt-5.5"]


def test_split_model_assignment_is_sticky_for_challenge():
    deps = _deps()

    first = select_model_specs_for_challenge(deps, "reverse-a")
    second = select_model_specs_for_challenge(deps, "reverse-a")

    assert first == second == ["codex/gpt-5.5"]
    assert deps.model_assignment_cursor == 1


def test_race_model_assignment_keeps_all_models():
    deps = _deps(split=False)

    assert select_model_specs_for_challenge(deps, "pwn-a") == [
        "codex/gpt-5.5",
        "deepseek/deepseek-flash",
    ]
    assert deps.model_assignment_cursor == 0
