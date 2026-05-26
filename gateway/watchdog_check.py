"""External watchdog checker for Hermes gateway liveness."""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone
from typing import Any

from gateway.status import get_running_pid, read_runtime_status


EXIT_RUNTIME_STATUS_UNREADABLE = 1
EXIT_DEAD_OR_MISMATCHED_PID = 2
EXIT_STALE_PROCESS_HEARTBEAT = 3
EXIT_STALE_FORWARD_PROGRESS = 4
EXIT_INVALID_PAYLOAD = 5
_SCHEMA_VERSION = 2
_ENV_FLOAT_ERRORS: list[str] = []


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return float(raw)
    except ValueError:
        _ENV_FLOAT_ERRORS.append(f"invalid numeric env {name}={raw!r}")
        return default


HEARTBEAT_INTERVAL = _env_float("HERMES_WATCHDOG_HEARTBEAT_INTERVAL", 15.0)
HEARTBEAT_STALE = _env_float("HERMES_WATCHDOG_HEARTBEAT_STALE", 45.0)
STARTUP_GRACE = _env_float("HERMES_WATCHDOG_STARTUP_GRACE", 90.0)
FORWARD_PROGRESS_TIMEOUT = _env_float(
    "HERMES_WATCHDOG_FORWARD_PROGRESS_TIMEOUT",
    _env_float("HERMES_AGENT_TIMEOUT", 1800.0),
)


def _fail(code: int, reason: str) -> int:
    print(reason, file=sys.stderr)
    return code


def _parse_wall_clock(value: Any) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _require_number(payload: dict[str, Any], key: str) -> float:
    value = payload.get(key)
    if value is None or isinstance(value, bool):
        raise ValueError(f"missing numeric field: {key}")
    return float(value)


def _payload_pid(payload: dict[str, Any]) -> int:
    value = payload.get("pid")
    if value is None or isinstance(value, bool):
        raise ValueError("missing numeric field: pid")
    return int(value)


def _load_runtime_status() -> dict[str, Any]:
    payload = read_runtime_status()
    if not isinstance(payload, dict):
        raise FileNotFoundError("runtime status missing or unreadable")
    return payload


def _validate_schema(payload: dict[str, Any]) -> None:
    if int(payload.get("gateway_status_schema_version") or 0) < _SCHEMA_VERSION:
        raise ValueError("runtime status schema too old")
    required = [
        "process_heartbeat_mono",
        "last_forward_progress_mono",
        "forward_progress_counter",
        "work_active",
        "run_lifecycle_count",
    ]
    for key in required:
        if key not in payload:
            raise ValueError(f"missing field: {key}")
    _payload_pid(payload)


def _startup_grace_active(payload: dict[str, Any]) -> bool:
    if str(payload.get("gateway_state") or "") != "starting":
        return False
    updated_at = _parse_wall_clock(payload.get("updated_at"))
    if updated_at is None:
        return False
    return (time.time() - updated_at) <= STARTUP_GRACE


def _live_pid_for_current_namespace() -> int | None:
    return get_running_pid(cleanup_stale=False)


def validate_runtime_payload_for_watchdog(
    payload: dict[str, Any],
    *,
    live_pid: int | None = None,
    now_mono: float | None = None,
) -> tuple[int, str]:
    state = str(payload.get("gateway_state") or "")
    if state in {"stopped", "stopping", "startup_failed"}:
        return EXIT_DEAD_OR_MISMATCHED_PID, f"gateway not running (state={state})"

    try:
        _validate_schema(payload)
        payload_pid = _payload_pid(payload)
    except (TypeError, ValueError) as exc:
        if _startup_grace_active(payload):
            return 0, ""
        return EXIT_INVALID_PAYLOAD, str(exc)

    if live_pid is not None and payload_pid != live_pid:
        return (
            EXIT_DEAD_OR_MISMATCHED_PID,
            f"gateway pid mismatch: runtime={payload_pid} live={live_pid}",
        )

    try:
        current_mono = time.monotonic() if now_mono is None else now_mono
        process_heartbeat_mono = _require_number(payload, "process_heartbeat_mono")
        process_heartbeat_age = current_mono - process_heartbeat_mono
        if process_heartbeat_age > HEARTBEAT_STALE:
            return (
                EXIT_STALE_PROCESS_HEARTBEAT,
                f"stale process heartbeat: age={process_heartbeat_age:.3f}s threshold={HEARTBEAT_STALE:.3f}s",
            )

        work_active = bool(payload.get("work_active"))
        if work_active:
            forward_progress_mono = _require_number(payload, "last_forward_progress_mono")
            forward_progress_age = current_mono - forward_progress_mono
            if forward_progress_age > FORWARD_PROGRESS_TIMEOUT:
                return (
                    EXIT_STALE_FORWARD_PROGRESS,
                    f"stale forward progress: age={forward_progress_age:.3f}s threshold={FORWARD_PROGRESS_TIMEOUT:.3f}s counter={payload.get('forward_progress_counter')}",
                )
    except ValueError as exc:
        return EXIT_INVALID_PAYLOAD, str(exc)

    return 0, ""


def main() -> int:
    if _ENV_FLOAT_ERRORS:
        return _fail(EXIT_INVALID_PAYLOAD, _ENV_FLOAT_ERRORS[0])

    try:
        payload = _load_runtime_status()
    except FileNotFoundError as exc:
        return _fail(EXIT_RUNTIME_STATUS_UNREADABLE, str(exc))

    code, reason = validate_runtime_payload_for_watchdog(
        payload,
        live_pid=_live_pid_for_current_namespace(),
    )
    if code:
        return _fail(code, reason)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
