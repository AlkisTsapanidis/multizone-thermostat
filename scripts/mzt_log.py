#!/usr/bin/env python3
"""Inspect Multizone Thermostat telemetry logs offline (stdlib only).

  python scripts/mzt_log.py summary <config>/mzt_telemetry
  python scripts/mzt_log.py csv <config>/mzt_telemetry --kind pid --zone climate.living -o living_pid.csv
  python scripts/mzt_log.py csv <files...> --kind boiler --since 2026-01-05T06:00 --until 2026-01-05T09:00
"""
import argparse
import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / "custom_components" / "multizone_thermostat"


def _load(name):
    # Loaded by path so Home Assistant is not needed (and the package __init__ is not imported).
    if name == "telemetry_export":
        _load("telemetry")
    spec = importlib.util.spec_from_file_location(name, PKG / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _ts(text):
    try:
        return float(text)
    except ValueError:
        dt = datetime.fromisoformat(text)
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()


def main(argv=None):
    ex = _load("telemetry_export")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("csv", "summary"):
        p = sub.add_parser(name)
        p.add_argument("paths", nargs="+", help="log files, globs or directories")
    c = sub.choices["csv"]
    c.add_argument("--kind")
    c.add_argument("--zone")
    c.add_argument("--since", type=_ts, help="UTC ISO time or epoch seconds")
    c.add_argument("--until", type=_ts)
    c.add_argument("-o", "--output", help="output file (default: stdout)")
    args = ap.parse_args(argv)

    records = ex.load(args.paths)
    if args.cmd == "summary":
        print(ex.summarize(records))
        return 0
    if args.output:
        with open(args.output, "w", newline="", encoding="utf-8") as fh:
            n = ex.export_csv(records, fh, kind=args.kind, zone=args.zone, since=args.since, until=args.until)
        print(f"wrote {n} rows to {args.output}", file=sys.stderr)
    else:
        ex.export_csv(records, sys.stdout, kind=args.kind, zone=args.zone, since=args.since, until=args.until)
    return 0


if __name__ == "__main__":
    sys.exit(main())
