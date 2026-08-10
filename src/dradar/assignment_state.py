"""Interpret the server's authoritative runner state with old-server fallback."""


_STATES = {"running", "paused", "resumable", "waiting"}


def assignment_state(assignment: dict) -> str:
    state = assignment.get("runner_state")
    if state in _STATES:
        return state
    if "heartbeat_running" in assignment:
        if assignment.get("heartbeat_running"):
            return "running"
        if assignment.get("execution_state") == "paused":
            return "paused"
        return "resumable" if assignment.get("started_at") else "waiting"
    # Compatibility with servers predating runner health in /assignment.
    return "running" if assignment.get("started_at") else "waiting"


def state_summary(assignments: list[dict]) -> str:
    counts = {state: 0 for state in ("running", "paused", "resumable", "waiting")}
    for assignment in assignments:
        counts[assignment_state(assignment)] += 1
    return ", ".join(
        f"{counts[state]} {state}" for state in counts if counts[state]
    )
