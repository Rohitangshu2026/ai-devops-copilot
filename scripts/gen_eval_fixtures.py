#!/usr/bin/env python3
"""Generate parameterized eval fixture variants from the 20 hand-authored archetypes.

Produces 80 derived scenarios by varying:
  1. service name        — sample-app, api-gateway, auth-service
  2. severity            — critical, high (preserving original if low/medium is correct)
  3. time pattern label  — sudden_spike, gradual_onset, persistent
  4. error_density label — low (5), medium (15), high (40)
  5. noise_level label   — clean, moderate, noisy

Output: evals/generated/<archetype>_<variant_id>/{logs.json,expected.json}

Usage:
    python scripts/gen_eval_fixtures.py [--dry-run]
"""
from __future__ import annotations

import argparse
import copy
import json
import random
import re
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INCIDENTS_DIR = ROOT / "evals" / "incidents"
GENERATED_DIR = ROOT / "evals" / "generated"

SERVICES = ["sample-app", "api-gateway", "auth-service"]
TIME_PATTERNS = ["sudden_spike", "gradual_onset", "persistent"]
ERROR_DENSITIES = [("low", 5), ("medium", 15), ("high", 40)]
NOISE_LEVELS = ["clean", "moderate", "noisy"]

_VARIANT_AXES = [
    ("sample-app",   "sudden_spike",  "low",    "clean"),
    ("api-gateway",  "gradual_onset", "medium", "moderate"),
    ("auth-service", "persistent",    "high",   "noisy"),
    ("api-gateway",  "sudden_spike",  "high",   "clean"),
]

BASE_TS = datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc)


def _shift_timestamps(logs: list[dict], offset_minutes: float) -> list[dict]:
    """Shift all @timestamp fields by offset_minutes."""
    result = []
    for log in logs:
        entry = copy.deepcopy(log)
        if "@timestamp" in entry:
            try:
                ts = datetime.fromisoformat(entry["@timestamp"].replace("Z", "+00:00"))
                ts = ts + timedelta(minutes=offset_minutes)
                entry["@timestamp"] = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
            except (ValueError, TypeError):
                pass
        result.append(entry)
    return result


def _add_noise(logs: list[dict], noise_level: str) -> list[dict]:
    """Add INFO health-check noise based on noise_level."""
    if noise_level == "clean":
        return logs

    noise_entries = [
        {
            "message": f"GET /health 200",
            "level": "INFO",
            "@timestamp": "2026-05-07T12:00:00Z",
            "endpoint": "/health",
            "status_code": 200,
            "service": logs[0].get("service", "sample-app") if logs else "sample-app",
            "environment": "dev",
        }
    ]

    count = 3 if noise_level == "moderate" else 8
    result = list(logs)
    for i in range(count):
        entry = copy.deepcopy(noise_entries[0])
        ts = BASE_TS + timedelta(seconds=i * 30)
        entry["@timestamp"] = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
        result.insert(i * 2, entry)
    return result


def _adjust_density(logs: list[dict], density_label: str, target_count: int) -> list[dict]:
    """Scale the log list to approximately target_count entries."""
    error_logs = [l for l in logs if l.get("level") in ("ERROR", "WARNING")]
    info_logs = [l for l in logs if l.get("level") not in ("ERROR", "WARNING")]

    if len(error_logs) == 0:
        return logs

    if target_count <= len(logs):
        # Trim: keep all errors, reduce info
        needed_info = max(0, target_count - len(error_logs))
        return error_logs + info_logs[:needed_info]
    else:
        # Expand: repeat error entries
        multiplier = (target_count // len(error_logs)) + 1
        expanded_errors = (error_logs * multiplier)[:target_count - len(info_logs)]
        return expanded_errors + info_logs


def _replace_service(logs: list[dict], service: str) -> list[dict]:
    """Replace the 'service' field in all log entries."""
    return [{**log, "service": service} for log in logs]


def generate_variants(dry_run: bool = False) -> int:
    """Generate all variants. Returns the count of variants written."""
    if not INCIDENTS_DIR.exists():
        print(f"ERROR: incidents dir not found: {INCIDENTS_DIR}", file=sys.stderr)
        return 0

    if not dry_run:
        if GENERATED_DIR.exists():
            shutil.rmtree(GENERATED_DIR)
        GENERATED_DIR.mkdir(parents=True)

    archetypes = sorted(p for p in INCIDENTS_DIR.iterdir() if p.is_dir())
    count = 0

    for archetype_dir in archetypes:
        logs_file = archetype_dir / "logs.json"
        expected_file = archetype_dir / "expected.json"
        if not logs_file.exists() or not expected_file.exists():
            continue

        base_logs = json.loads(logs_file.read_text())
        base_expected = json.loads(expected_file.read_text())

        for var_idx, (service, time_pattern, density_label, noise_level) in enumerate(_VARIANT_AXES):
            target_count = dict(ERROR_DENSITIES)[density_label]

            variant_logs = copy.deepcopy(base_logs)
            variant_logs = _replace_service(variant_logs, service)
            variant_logs = _adjust_density(variant_logs, density_label, target_count)
            variant_logs = _add_noise(variant_logs, noise_level)

            # Shift timestamps for time_pattern variety
            offset = {"sudden_spike": 0, "gradual_onset": -15, "persistent": -30}[time_pattern]
            variant_logs = _shift_timestamps(variant_logs, offset)

            variant_expected = copy.deepcopy(base_expected)
            variant_expected["service"] = service
            variant_expected["time_pattern"] = time_pattern
            variant_expected["density"] = density_label
            variant_expected["noise_level"] = noise_level
            variant_expected["_archetype"] = archetype_dir.name

            variant_id = f"{archetype_dir.name}_v{var_idx + 1:02d}"
            out_dir = GENERATED_DIR / variant_id

            if dry_run:
                print(f"  [DRY RUN] Would write: {variant_id}/")
            else:
                out_dir.mkdir(parents=True)
                (out_dir / "logs.json").write_text(json.dumps(variant_logs, indent=2))
                (out_dir / "expected.json").write_text(json.dumps(variant_expected, indent=2))
                print(f"  Generated: {variant_id}/")

            count += 1

    return count


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Show what would be generated without writing files")
    args = parser.parse_args()

    print(f"Generating eval variants from {len(list(INCIDENTS_DIR.iterdir()))} archetypes...")
    count = generate_variants(dry_run=args.dry_run)
    print(f"\n{'[DRY RUN] Would generate' if args.dry_run else 'Generated'} {count} variant fixtures.")

    if not args.dry_run:
        print(f"Output: {GENERATED_DIR}")


if __name__ == "__main__":
    main()
