"""Tests for the read-only Home Assistant capture tool."""
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "mzt_capture.py"
spec = importlib.util.spec_from_file_location("mzt_capture", SCRIPT)
capture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capture)


def state(entity, value, **attributes):
    return {"entity_id": entity, "state": value, "attributes": attributes}


def events(stream):
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def test_monitor_reports_changes_candidates_and_unavailable():
    stream = io.StringIO()
    monitor = capture.Monitor(["switch.boiler", "switch.a", "switch.b"], "switch.boiler",
                              ["switch.a", "switch.b"], 2, stream)
    monitor.update({s["entity_id"]: s for s in [state("switch.boiler", "on"),
                   state("switch.a", "off"), state("switch.b", "off")]}, 0)
    assert events(stream)[-1]["status"] == "reported_boiler_on_all_selected_valves_off"
    monitor.update({s["entity_id"]: s for s in [state("switch.boiler", "on"),
                   state("switch.a", "on"), state("switch.b", "off")]}, 10)
    monitor.update({s["entity_id"]: s for s in [state("switch.boiler", "on"),
                   state("switch.a", "off"), state("switch.b", "off")]}, 20)
    assert any(e["type"] == "candidate_rapid_transitions" and e["entity_id"] == "switch.a"
               for e in events(stream))
    monitor.update({s["entity_id"]: s for s in [state("switch.boiler", "on"),
                   state("switch.a", "off"), state("switch.b", "unavailable")]}, 30)
    assert events(stream)[-1]["status"] == "unknown"
    monitor.update({"switch.boiler": state("switch.boiler", "on"),
                    "switch.a": state("switch.a", "off")}, 40)
    assert any(e["type"] == "state" and e["entity_id"] == "switch.b" and e["value"] is None
               for e in events(stream))


def test_gap_resets_transition_counts_and_logs_fresh_snapshot():
    stream = io.StringIO()
    monitor = capture.Monitor(["switch.boiler", "switch.a"], "switch.boiler", ["switch.a"], 2, stream)
    monitor.update({"switch.boiler": state("switch.boiler", "on"), "switch.a": state("switch.a", "on")}, 0)
    monitor.update({"switch.boiler": state("switch.boiler", "on"), "switch.a": state("switch.a", "off")}, 1)
    monitor.gap()
    monitor.update({"switch.boiler": state("switch.boiler", "on"), "switch.a": state("switch.a", "on")}, 2)
    assert not any(e["type"] == "candidate_rapid_transitions" for e in events(stream))
    assert sum(e["type"] == "state" and e["entity_id"] == "switch.a" for e in events(stream)) == 3


def test_missing_entity_breaks_transition_history():
    stream = io.StringIO()
    monitor = capture.Monitor(["switch.boiler", "switch.a"], "switch.boiler", ["switch.a"], 2, stream)
    boiler = state("switch.boiler", "on")
    monitor.update({"switch.boiler": boiler, "switch.a": state("switch.a", "on")}, 0)
    monitor.update({"switch.boiler": boiler, "switch.a": state("switch.a", "off")}, 1)
    monitor.update({"switch.boiler": boiler}, 2)
    monitor.update({"switch.boiler": boiler, "switch.a": state("switch.a", "on")}, 3)
    monitor.update({"switch.boiler": boiler, "switch.a": state("switch.a", "off")}, 4)
    assert not any(e["type"] == "candidate_rapid_transitions" for e in events(stream))


def test_copy_preserves_rotated_files_and_does_not_overwrite(tmp_path):
    src = tmp_path / "source"
    src.mkdir()
    (src / "mzt_2026-01-01.jsonl").write_text("first\n", encoding="utf-8")
    (src / "mzt_2026-01-01.1.jsonl").write_text("second\n", encoding="utf-8")
    stream = io.StringIO()
    capture.copy_telemetry(src, tmp_path / "copies", stream)
    (src / "mzt_2026-01-01.jsonl").write_text("updated\n", encoding="utf-8")
    capture.copy_telemetry(src, tmp_path / "copies", stream)
    snapshots = sorted((tmp_path / "copies").iterdir())
    assert len(snapshots) == 2
    assert (snapshots[0] / "mzt_2026-01-01.jsonl").read_text() == "first\n"
    assert (snapshots[1] / "mzt_2026-01-01.jsonl").read_text() == "updated\n"
    assert (snapshots[0] / "mzt_2026-01-01.1.jsonl").read_text() == "second\n"
    assert len(events(stream)) == 2


def test_missing_telemetry_is_explicit(tmp_path):
    with pytest.raises(OSError, match="does not exist"):
        capture.copy_telemetry(tmp_path / "missing", tmp_path / "copies", io.StringIO())


def test_cli_read_only_end_to_end_and_markers(tmp_path):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, self.headers.get("Authorization")))
            body = json.dumps([state("switch.boiler", "on"), state("switch.valve", "off"),
                               state("sensor.room", "20.5", unit_of_measurement="°C")]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    output = tmp_path / "capture"
    telemetry = tmp_path / "logs"
    telemetry.mkdir()
    (telemetry / "mzt_2026-01-01.jsonl").write_text('{"kind":"pid"}\n')
    env = dict(os.environ, HA_TOKEN="test-token")
    try:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--url", f"http://127.0.0.1:{server.server_port}",
             "--allow-http", "--output", str(output), "--boiler", "switch.boiler",
             "--valve", "switch.valve", "--entity", "sensor.room", "--interval", "0.05",
             "--duration", "0.25", "--telemetry-dir", str(telemetry)],
            input="start experiment\n", text=True, capture_output=True, env=env, timeout=10,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert result.returncode == 0, result.stderr
    recorded = [json.loads(line) for line in (output / "observations.jsonl").read_text(encoding="utf-8").splitlines()]
    assert requests and all(path == "/api/states" and auth == "Bearer test-token" for path, auth in requests)
    assert any(e["type"] == "marker" and e["text"] == "start experiment" for e in recorded)
    assert any(e.get("status") == "reported_boiler_on_all_selected_valves_off" for e in recorded)
    assert any(e["type"] == "state" and e["entity_id"] == "sensor.room" for e in recorded)
    assert sum(e["type"] == "telemetry_copy" for e in recorded) == 2
    assert recorded[-1]["type"] == "stop"
    assert "test-token" not in (output / "observations.jsonl").read_text(encoding="utf-8")


def test_existing_output_directory_is_not_overwritten(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HA_TOKEN", "secret")
    assert capture.main(["--url", "https://ha.example", "--output", str(tmp_path),
                         "--entity", "sensor.room"]) == 1
    assert "Capture failed" in capsys.readouterr().err


def test_http_requires_explicit_opt_in(tmp_path, monkeypatch):
    monkeypatch.setenv("HA_TOKEN", "secret")
    with pytest.raises(SystemExit) as exc:
        capture.main(["--url", "http://ha.example", "--output", str(tmp_path / "out"),
                      "--entity", "sensor.room"])
    assert exc.value.code == 2
    assert not (tmp_path / "out").exists()


def test_authentication_failure_never_exposes_token(tmp_path):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(401)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    output = tmp_path / "capture"
    try:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--url", f"http://127.0.0.1:{server.server_port}",
             "--allow-http", "--output", str(output), "--entity", "sensor.room",
             "--interval", "0.05", "--duration", "0.15"],
            input="", text=True, capture_output=True, env=dict(os.environ, HA_TOKEN="secret-token"),
            timeout=10,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert result.returncode == 1
    content = (output / "observations.jsonl").read_text(encoding="utf-8")
    recorded = [json.loads(line) for line in content.splitlines()]
    assert any(e["type"] == "poll_error" and "401" in e["error"] for e in recorded)
    assert recorded[-1]["connection_unavailable"]
    assert "secret-token" not in content + result.stderr + result.stdout
