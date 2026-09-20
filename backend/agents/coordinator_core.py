"""Shared coordinator tool logic — called by both Claude SDK and Codex coordinators."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from backend.deps import CoordinatorDeps
from backend.flag_validation import validate_flag_candidate
from backend.message_bus import CoordinatorEvent, CoordinatorEventType
from backend.prompts import ChallengeMeta
from backend.solver_base import FLAG_FOUND
from backend.task_registry import TaskStatus

logger = logging.getLogger(__name__)


async def do_fetch_challenges(deps: CoordinatorDeps) -> str:
    challenges = await deps.ctfd.fetch_all_challenges()
    solved = await deps.ctfd.fetch_solved_names()
    result = []
    for ch in challenges:
        name = ch.get("name", "?")
        status = "SOLVED" if name in solved else "unsolved"
        if deps.task_registry:
            task = deps.task_registry.register(
                name,
                category=ch.get("category", "?"),
                value=ch.get("value", 0),
                solves=ch.get("solves", 0),
            )
            if name in solved:
                deps.task_registry.finish(name, TaskStatus.SOLVED)
            elif task.status == TaskStatus.DISCOVERED:
                deps.task_registry.queue(name)
        result.append({
            "name": ch.get("name", "?"),
            "category": ch.get("category", "?"),
            "value": ch.get("value", 0),
            "solves": ch.get("solves", 0),
            "status": status,
            "description": (ch.get("description") or "")[:200],
        })
    return json.dumps(result, indent=2)


async def do_get_solve_status(deps: CoordinatorDeps) -> str:
    solved = await deps.ctfd.fetch_solved_names()
    swarm_status = {name: swarm.get_status() for name, swarm in deps.swarms.items()}
    registry = deps.task_registry.snapshot() if deps.task_registry else []
    return json.dumps(
        {"solved": sorted(solved), "active_swarms": swarm_status, "task_registry": registry},
        indent=2,
    )


async def do_spawn_swarm(deps: CoordinatorDeps, challenge_name: str) -> str:
    # Retire ALL finished swarms before checking capacity
    finished = [
        name for name, swarm in deps.swarms.items()
        if swarm.cancel_event.is_set()
        or (name in deps.swarm_tasks and deps.swarm_tasks[name].done())
    ]
    for name in finished:
        del deps.swarms[name]
        deps.swarm_tasks.pop(name, None)

    active_count = len(deps.swarms)
    if active_count >= deps.max_concurrent_challenges:
        return f"At capacity ({active_count}/{deps.max_concurrent_challenges} challenges running). Wait for one to finish."

    if challenge_name in deps.swarms:
        return f"Swarm still running for {challenge_name}"

    if deps.task_registry:
        task = deps.task_registry.register(challenge_name)
        if task.status == TaskStatus.SOLVED:
            return f"Challenge {challenge_name} is already solved"

    # Auto-pull challenge if needed
    if challenge_name not in deps.challenge_dirs:
        challenges = await deps.ctfd.fetch_all_challenges()
        ch_data = next((c for c in challenges if c.get("name") == challenge_name), None)
        if not ch_data:
            return f"Challenge '{challenge_name}' not found on CTFd"
        output_dir = str(Path(deps.challenges_root))
        ch_dir = await deps.ctfd.pull_challenge(ch_data, output_dir)
        deps.challenge_dirs[challenge_name] = ch_dir
        deps.challenge_metas[challenge_name] = ChallengeMeta.from_yaml(Path(ch_dir) / "metadata.yml")

    meta = deps.challenge_metas[challenge_name]
    if deps.task_registry:
        deps.task_registry.start(
            challenge_name,
            path=deps.challenge_dirs[challenge_name],
            category=meta.category,
        )
    if deps.event_bus:
        await deps.event_bus.publish(CoordinatorEvent(
            CoordinatorEventType.TASK_STARTED,
            challenge_name,
            payload={"path": deps.challenge_dirs[challenge_name], "category": meta.category},
        ))

    from backend.agents.swarm import ChallengeSwarm

    swarm = ChallengeSwarm(
        challenge_dir=deps.challenge_dirs[challenge_name],
        meta=meta,
        ctfd=deps.ctfd,
        cost_tracker=deps.cost_tracker,
        settings=deps.settings,
        model_specs=deps.model_specs,
        no_submit=deps.no_submit,
        coordinator_inbox=deps.coordinator_inbox,
        coordinator_event_bus=deps.event_bus,
    )
    deps.swarms[challenge_name] = swarm

    async def _run_and_cleanup() -> None:
        try:
            result = await swarm.run()
            # Flag already submitted/confirmed by solver's submit_fn — just record the result
            if result and result.status == FLAG_FOUND:
                deps.results[challenge_name] = {
                    "flag": result.flag,
                    "submit": "DRY RUN" if deps.no_submit else "confirmed by solver",
                }
                if deps.task_registry:
                    deps.task_registry.finish(challenge_name, TaskStatus.SOLVED)
                if deps.event_bus:
                    await deps.event_bus.publish(CoordinatorEvent(
                        CoordinatorEventType.TASK_SOLVED,
                        challenge_name,
                        source="swarm",
                        payload={"flag": result.flag},
                    ))
                return

            attempts = 1
            if deps.task_registry:
                current_task = deps.task_registry.register(challenge_name)
                if current_task.status != TaskStatus.RUNNING:
                    return
                attempts = current_task.attempts
            max_attempts = getattr(deps.settings, "max_attempts_per_challenge", 3)
            final_status = TaskStatus.BLOCKED if attempts >= max_attempts else TaskStatus.QUEUED
            if deps.task_registry:
                deps.task_registry.finish(
                    challenge_name,
                    final_status,
                    "swarm exhausted without a confirmed flag",
                )
            if deps.event_bus:
                event_kind = (
                    CoordinatorEventType.TASK_BLOCKED
                    if final_status == TaskStatus.BLOCKED
                    else CoordinatorEventType.TASK_FAILED
                )
                await deps.event_bus.publish(CoordinatorEvent(
                    event_kind,
                    challenge_name,
                    source="swarm",
                    payload={"attempts": attempts, "will_retry": final_status == TaskStatus.QUEUED},
                ))
        except Exception as exc:
            logger.exception("Swarm failed for %s", challenge_name)
            if (
                deps.task_registry
                and deps.task_registry.register(challenge_name).status == TaskStatus.RUNNING
            ):
                deps.task_registry.finish(challenge_name, TaskStatus.FAILED, str(exc))
            if deps.event_bus:
                await deps.event_bus.publish(CoordinatorEvent(
                    CoordinatorEventType.TASK_FAILED,
                    challenge_name,
                    source="swarm",
                    payload={"error": str(exc), "will_retry": False},
                ))

    task = asyncio.create_task(_run_and_cleanup(), name=f"swarm-{challenge_name}")
    deps.swarm_tasks[challenge_name] = task
    return f"Swarm spawned for {challenge_name} with {len(deps.model_specs)} models"


async def do_check_swarm_status(deps: CoordinatorDeps, challenge_name: str) -> str:
    swarm = deps.swarms.get(challenge_name)
    if not swarm:
        return f"No swarm running for {challenge_name}"
    return json.dumps(swarm.get_status(), indent=2)


async def do_submit_flag(deps: CoordinatorDeps, challenge_name: str, flag: str) -> str:
    valid, reason, normalized = validate_flag_candidate(flag)
    if not valid:
        return f"REJECTED — {reason}."
    if deps.event_bus:
        await deps.event_bus.publish(CoordinatorEvent(
            CoordinatorEventType.FLAG_CANDIDATE,
            challenge_name,
            payload={"candidate_length": len(normalized)},
        ))
    if deps.no_submit:
        return f'DRY RUN — would submit "{normalized}" for {challenge_name}'
    try:
        result = await deps.ctfd.submit_flag(challenge_name, normalized)
        if result.status in ("correct", "already_solved"):
            if deps.task_registry:
                deps.task_registry.finish(challenge_name, TaskStatus.SOLVED)
            swarm = deps.swarms.get(challenge_name)
            if swarm:
                swarm.kill()
            if deps.event_bus:
                await deps.event_bus.publish(CoordinatorEvent(
                    CoordinatorEventType.TASK_SOLVED,
                    challenge_name,
                    payload={"verified_by": "ctfd"},
                ))
        return result.display
    except Exception as e:
        return f"submit_flag error: {e}"


async def do_kill_swarm(deps: CoordinatorDeps, challenge_name: str) -> str:
    swarm = deps.swarms.get(challenge_name)
    if not swarm:
        return f"No swarm running for {challenge_name}"
    swarm.kill()
    if deps.task_registry:
        deps.task_registry.finish(challenge_name, TaskStatus.BLOCKED, "cancelled by coordinator")
    return f"Swarm for {challenge_name} cancelled"


async def do_bump_agent(deps: CoordinatorDeps, challenge_name: str, model_spec: str, insights: str) -> str:
    swarm = deps.swarms.get(challenge_name)
    if not swarm:
        return f"No swarm running for {challenge_name}"
    solver = swarm.solvers.get(model_spec)
    if not solver:
        return f"No solver for {model_spec} in {challenge_name}"
    solver.bump(insights)
    if deps.event_bus:
        await deps.event_bus.publish(CoordinatorEvent(
            CoordinatorEventType.HELP_ACCEPTED,
            challenge_name,
            payload={"model": model_spec, "insights": insights[:500]},
        ))
    return f"Bumped {model_spec} on {challenge_name}"


async def do_read_solver_trace(deps: CoordinatorDeps, challenge_name: str, model_spec: str, last_n: int = 20) -> str:
    """Read the last N trace events from a solver's JSONL log."""
    swarm = deps.swarms.get(challenge_name)
    if not swarm:
        return f"No swarm for {challenge_name}"
    solver = swarm.solvers.get(model_spec)
    if not solver:
        return f"No solver for {model_spec}"
    trace_path = getattr(solver, "tracer", None)
    if not trace_path:
        return "No tracer on solver"
    path = trace_path.path if hasattr(trace_path, "path") else str(trace_path)
    try:
        lines = Path(path).read_text().strip().split("\n")
        recent = lines[-last_n:]
        summary = []
        for line in recent:
            try:
                d = json.loads(line)
                t = d.get("type", "?")
                if t == "tool_call":
                    args_str = str(d.get("args", ""))[:100]
                    summary.append(f"step {d.get('step','?')} CALL {d.get('tool','?')}: {args_str}")
                elif t == "tool_result":
                    result_str = str(d.get("result", ""))[:100]
                    summary.append(f"step {d.get('step','?')} RESULT {d.get('tool','?')}: {result_str}")
                elif t in ("finish", "error", "bump", "turn_failed"):
                    summary.append(f"** {t}: {json.dumps({k:v for k,v in d.items() if k != 'ts'})}")
                elif t == "usage":
                    summary.append(f"usage: in={d.get('input_tokens',0)} out={d.get('output_tokens',0)} cost=${d.get('cost_usd',0):.4f}")
                else:
                    summary.append(f"{t}: {str(d)[:80]}")
            except Exception:
                summary.append(line[:100])
        return "\n".join(summary)
    except FileNotFoundError:
        return f"Trace file not found: {path}"
    except Exception as e:
        return f"Error reading trace: {e}"


async def do_broadcast(deps: CoordinatorDeps, challenge_name: str, message: str) -> str:
    """Broadcast a message to all solvers working on a challenge."""
    swarm = deps.swarms.get(challenge_name)
    if not swarm:
        return f"No swarm running for {challenge_name}"
    await swarm.message_bus.broadcast(message)
    return f"Broadcast to all solvers on {challenge_name}"
