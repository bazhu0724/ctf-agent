# Portable competition runbook

Current instructions: [README.md](README.md) and [AGENT_HANDOFF.md](docs/AGENT_HANDOFF.md).

1. Install from the committed lock: `uv sync --locked`.
2. Configure Magpie and local `.env`.
3. Run `uv run --locked ctf-doctor --live --json`.
4. The outer agent schedules at most three independent single-challenge runs.
5. Use `--no-submit` for reproduction; submit according to the competition task.
6. Preserve outputs, write-ups and handoff notes before stopping.

The optional internal scheduler requires explicit `--coordinator codex` or `claude`. Solver cost estimates exclude coordinator billing. No personal machine paths or local installation status are assumed.
