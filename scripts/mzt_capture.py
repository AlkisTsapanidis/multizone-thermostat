#!/usr/bin/env python3
"""Passively capture selected Home Assistant states and optional MZT telemetry files."""
from __future__ import annotations

import argparse
from collections import deque
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import queue
import shutil
import sys
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def positive(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return number


def reject_nonfinite(value):
    raise ValueError(f"invalid JSON number: {value}")


def fetch_states(url, token, timeout):
    request = Request(url.rstrip("/") + "/api/states", headers={"Authorization": "Bearer " + token})
    try:
        with build_opener(NoRedirect()).open(request, timeout=timeout) as response:
            data = json.load(response, parse_constant=reject_nonfinite)
    except HTTPError as exc:
        raise RuntimeError(f"Home Assistant returned HTTP {exc.code}") from exc
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        raise RuntimeError(f"Home Assistant request failed ({type(exc).__name__})") from exc
    if not isinstance(data, list) or any(
        not isinstance(item, dict) or not isinstance(item.get("entity_id"), str)
        or not isinstance(item.get("state"), str) or not isinstance(item.get("attributes"), dict)
        for item in data
    ):
        raise RuntimeError("Home Assistant returned an invalid state list")
    return {item["entity_id"]: item for item in data}


def write_event(stream, kind, **data):
    event = {"time": utc_now(), "type": kind, **data}
    stream.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n")
    stream.flush()


def copy_telemetry(source, destination, stream):
    if not source.is_dir():
        raise OSError(f"Telemetry directory does not exist: {source}")
    files = sorted(source.glob("mzt_*.jsonl"))
    if not files:
        raise OSError(f"No MZT JSONL files found in {source}")
    snapshot = destination / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    snapshot.mkdir(parents=True, exist_ok=False)
    for path in files:
        if not path.is_file():
            raise OSError(f"Telemetry source is not a file: {path}")
        shutil.copyfile(path, snapshot / path.name)
    write_event(stream, "telemetry_copy", directory=str(snapshot), files=[p.name for p in files])


class Monitor:
    def __init__(self, entities, boiler, valves, threshold, stream):
        self.entities = entities
        self.boiler = boiler
        self.valves = valves
        self.threshold = threshold
        self.stream = stream
        self.previous = {}
        self.transitions = {entity: deque() for entity in ([boiler] if boiler else []) + valves}
        self.candidate = None

    def gap(self):
        self.previous.clear()
        self.candidate = None
        for transitions in self.transitions.values():
            transitions.clear()

    def update(self, states, monotonic):
        selected = {entity: states.get(entity) for entity in self.entities}
        for entity, current in selected.items():
            previous = self.previous.get(entity)
            if entity not in self.previous or current != previous:
                write_event(self.stream, "state", entity_id=entity, value=current)
                if current is None:
                    print(f"WARNING: entity missing: {entity}", file=sys.stderr, flush=True)
            if entity in self.transitions and current is None:
                self.transitions[entity].clear()
            if entity in self.transitions and previous is not None and current is not None:
                before, after = previous["state"], current["state"]
                if before in ("on", "off") and after in ("on", "off") and before != after:
                    times = self.transitions[entity]
                    times.append(monotonic)
                    while times and monotonic - times[0] > 3600:
                        times.popleft()
                    if len(times) >= self.threshold:
                        write_event(self.stream, "candidate_rapid_transitions", entity_id=entity,
                                    transitions_last_hour=len(times), window_seconds=3600)
                        print(f"Candidate rapid transitions: {entity} ({len(times)} in 1h)", flush=True)
                elif after not in ("on", "off"):
                    self.transitions[entity].clear()
        self.previous = selected
        if self.boiler and self.valves:
            boiler = selected[self.boiler]
            valve_states = [selected[valve] for valve in self.valves]
            if boiler is None or boiler["state"] not in ("on", "off") or any(
                item is None or item["state"] not in ("on", "off") for item in valve_states
            ):
                status = "unknown"
            elif boiler["state"] == "on" and all(item["state"] == "off" for item in valve_states):
                status = "reported_boiler_on_all_selected_valves_off"
            else:
                status = "not_observed"
            if status != self.candidate:
                write_event(self.stream, "candidate_closed_valves", status=status,
                            boiler=self.boiler, valves=self.valves)
                if status == "reported_boiler_on_all_selected_valves_off":
                    print("Candidate: reported boiler on and all selected valves off; "
                          "physical firing, valve position and flow are not established.", flush=True)
                self.candidate = status


def read_markers(markers):
    for line in sys.stdin:
        text = line.strip()
        if text:
            markers.put(text)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="Home Assistant base URL")
    parser.add_argument("--allow-http", action="store_true", help="allow unencrypted HTTP on a trusted network")
    parser.add_argument("--output", type=Path, required=True, help="new, private capture directory")
    parser.add_argument("--entity", action="append", default=[], help="entity ID to monitor; repeat")
    parser.add_argument("--boiler", help="reported boiler on/off entity")
    parser.add_argument("--valve", action="append", default=[], help="reported on=open valve; repeat")
    parser.add_argument("--interval", type=positive, default=10, help="poll interval in seconds (default 10)")
    parser.add_argument("--duration", type=positive, help="capture duration in seconds (default: until Ctrl+C)")
    parser.add_argument("--timeout", type=positive, default=10, help="HTTP timeout seconds (default 10)")
    parser.add_argument("--rapid-threshold", type=int, default=6, help="on/off transitions per hour (default 6)")
    parser.add_argument("--telemetry-dir", type=Path, help="local or mounted HA mzt_telemetry directory")
    parser.add_argument("--copy-every", type=positive, default=3600, help="snapshot interval seconds (default 3600)")
    args = parser.parse_args(argv)
    url = urlsplit(args.url)
    if url.scheme not in ("http", "https") or not url.netloc or url.username or url.password or url.query or url.fragment:
        parser.error("--url must be an HTTP(S) base URL without credentials, query or fragment")
    if url.scheme == "http" and not args.allow_http:
        parser.error("HTTP exposes the token in transit; use HTTPS or explicitly pass --allow-http")
    if args.rapid_threshold < 1:
        parser.error("--rapid-threshold must be at least 1")
    if bool(args.boiler) != bool(args.valve):
        parser.error("--boiler and at least one --valve must be supplied together")
    entities = list(dict.fromkeys(args.entity + ([args.boiler] if args.boiler else []) + args.valve))
    if not entities or any(not entity or "." not in entity for entity in entities):
        parser.error("specify valid entity IDs with --entity, or --boiler and --valve")
    if args.boiler and (args.boiler in args.valve or len(args.valve) != len(set(args.valve))):
        parser.error("boiler and valve entities must be distinct")
    token = os.environ.get("HA_TOKEN")
    if not token:
        parser.error("set HA_TOKEN to a Home Assistant long-lived access token")
    try:
        args.output.mkdir(parents=True, exist_ok=False)
        with (args.output / "observations.jsonl").open("x", encoding="utf-8") as stream:
            write_event(stream, "start", entities=entities, boiler=args.boiler, valves=args.valve,
                        interval_seconds=args.interval, rapid_threshold=args.rapid_threshold,
                        note="Polled reported states; changes between polls may be missed.")
            markers = queue.Queue()
            threading.Thread(target=read_markers, args=(markers,), daemon=True).start()
            monitor = Monitor(entities, args.boiler, args.valve, args.rapid_threshold, stream)
            start = time.monotonic()
            next_copy = start
            failed = False
            print("Capturing read-only states. Enter a line to add a marker; Ctrl+C stops.", flush=True)
            try:
                while True:
                    now = time.monotonic()
                    if args.duration is not None and now - start >= args.duration:
                        break
                    while not markers.empty():
                        write_event(stream, "marker", text=markers.get_nowait())
                    try:
                        states = fetch_states(args.url, token, args.timeout)
                    except RuntimeError as exc:
                        if not failed:
                            write_event(stream, "poll_error", error=str(exc))
                            print(f"WARNING: {exc}; retrying", file=sys.stderr, flush=True)
                            monitor.gap()
                        failed = True
                    else:
                        if failed:
                            write_event(stream, "poll_recovered")
                            print("Home Assistant connection recovered.", flush=True)
                        failed = False
                        monitor.update(states, time.monotonic())
                    now = time.monotonic()
                    if args.telemetry_dir and now >= next_copy:
                        copy_telemetry(args.telemetry_dir, args.output / "telemetry", stream)
                        next_copy = time.monotonic() + args.copy_every
                    wait = args.interval
                    if args.duration is not None:
                        wait = min(wait, max(0, args.duration - (time.monotonic() - start)))
                    time.sleep(wait)
            except KeyboardInterrupt:
                print("\nStopping capture.", flush=True)
            finally:
                while not markers.empty():
                    write_event(stream, "marker", text=markers.get_nowait())
                if args.telemetry_dir:
                    copy_telemetry(args.telemetry_dir, args.output / "telemetry", stream)
                write_event(stream, "stop", connection_unavailable=failed)
            if failed:
                print("Capture ended while Home Assistant was unreachable.", file=sys.stderr)
                return 1
    except OSError as exc:
        print(f"Capture failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
