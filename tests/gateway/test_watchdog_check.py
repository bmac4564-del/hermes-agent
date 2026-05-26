"""Tests for the external gateway watchdog checker."""

import importlib


def _runtime_payload(**overrides):
    payload = {
        "pid": 123,
        "kind": "hermes-gateway",
        "gateway_state": "running",
        "gateway_status_schema_version": 2,
        "process_heartbeat_mono": 100.0,
        "last_forward_progress_mono": 100.0,
        "forward_progress_counter": 1,
        "work_active": False,
        "run_lifecycle_count": 1,
        "updated_at": "2026-05-26T00:00:00+00:00",
    }
    payload.update(overrides)
    return payload


def test_watchdog_accepts_fresh_runtime_when_pid_scan_is_unavailable(monkeypatch):
    watchdog = importlib.import_module("gateway.watchdog_check")

    monkeypatch.setattr(watchdog, "read_runtime_status", lambda: _runtime_payload())
    monkeypatch.setattr(watchdog, "get_running_pid", lambda cleanup_stale=False: None)
    monkeypatch.setattr(watchdog.time, "monotonic", lambda: 110.0)

    assert watchdog.main() == 0


def test_watchdog_rejects_mismatched_visible_pid(monkeypatch):
    watchdog = importlib.import_module("gateway.watchdog_check")

    monkeypatch.setattr(watchdog, "read_runtime_status", lambda: _runtime_payload(pid=123))
    monkeypatch.setattr(watchdog, "get_running_pid", lambda cleanup_stale=False: 456)

    assert watchdog.main() == watchdog.EXIT_DEAD_OR_MISMATCHED_PID


def test_watchdog_rejects_stale_forward_progress(monkeypatch):
    watchdog = importlib.import_module("gateway.watchdog_check")

    monkeypatch.setattr(
        watchdog,
        "read_runtime_status",
        lambda: _runtime_payload(work_active=True, last_forward_progress_mono=50.0),
    )
    monkeypatch.setattr(watchdog, "get_running_pid", lambda cleanup_stale=False: 123)
    monkeypatch.setattr(watchdog, "FORWARD_PROGRESS_TIMEOUT", 10.0)
    monkeypatch.setattr(watchdog.time, "monotonic", lambda: 100.0)

    assert watchdog.main() == watchdog.EXIT_STALE_FORWARD_PROGRESS


def test_watchdog_reports_invalid_numeric_env_without_traceback(monkeypatch):
    watchdog = importlib.import_module("gateway.watchdog_check")

    monkeypatch.setattr(watchdog, "_ENV_FLOAT_ERRORS", ["invalid numeric env HERMES_AGENT_TIMEOUT='slow'"])

    assert watchdog.main() == watchdog.EXIT_INVALID_PAYLOAD
