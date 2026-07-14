#!/usr/bin/env python3
"""Validate the current Kolla JSON summary against one frozen native build."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

IMAGE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
BUCKETS = ("built", "failed", "not_matched", "skipped", "unbuildable")
ENTRY_KEYS = {
    "built": {"name"},
    "failed": {"name", "status"},
    "not_matched": {"name"},
    "skipped": {"name"},
    "unbuildable": {"name"},
}
FAILED_STATUSES = {"connection_error", "error", "parent_error", "push_error"}


def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as file_obj:
        return json.load(file_obj, object_pairs_hook=reject_duplicate_keys)


def planned_build(plan: dict[str, Any], arch: str) -> tuple[set[str], Path]:
    build = plan.get("build")
    if type(build) is not dict:
        raise ValueError("frozen plan build must be an object")
    architectures = build.get("architectures")
    if type(architectures) is not list:
        raise ValueError("frozen plan build architectures must be a list")
    matches = []
    for entry in architectures:
        if type(entry) is not dict:
            raise ValueError("frozen plan architecture entry must be an object")
        if entry.get("arch") == arch:
            matches.append(entry)
    if len(matches) != 1:
        raise ValueError(f"frozen plan must contain exactly one {arch} build")
    architecture = matches[0]
    names: list[str] = []
    for bucket in ("parents", "images"):
        entries = architecture.get(bucket)
        if type(entries) is not list:
            raise ValueError(f"frozen plan {arch} {bucket} must be a list")
        for entry in entries:
            if type(entry) is not dict or type(entry.get("image")) is not str:
                raise ValueError(f"frozen plan {arch} {bucket} entry is invalid")
            names.append(entry["image"])
    if len(names) != len(set(names)):
        raise ValueError(f"frozen plan {arch} build names must be unique")
    commands = architecture.get("commands")
    if type(commands) is not dict:
        raise ValueError(f"frozen plan {arch} commands must be an object")
    command = commands.get("kolla_build_push")
    if type(command) is not list or not all(type(part) is str for part in command):
        raise ValueError("frozen Kolla command must be a string argv list")
    positions = [index for index, part in enumerate(command) if part == "--summary-json-file"]
    if len(positions) != 1 or positions[0] + 1 >= len(command):
        raise ValueError("frozen Kolla command must contain one summary path")
    return set(names), Path(command[positions[0] + 1])


def validate_summary(summary: Any, expected: set[str]) -> list[str]:
    if type(summary) is not dict:
        return ["Kolla build summary must be an object"]
    errors: list[str] = []
    if set(summary) != set(BUCKETS):
        errors.append(f"Kolla build summary keys must be exactly {sorted(BUCKETS)!r}")
    names_by_bucket: dict[str, set[str]] = {}
    all_names: dict[str, str] = {}
    for bucket in BUCKETS:
        entries = summary.get(bucket)
        if type(entries) is not list:
            errors.append(f"{bucket} must be a list")
            names_by_bucket[bucket] = set()
            continue
        names: set[str] = set()
        for index, entry in enumerate(entries):
            if type(entry) is not dict or set(entry) != ENTRY_KEYS[bucket]:
                errors.append(
                    f"{bucket}[{index}] keys must be exactly {sorted(ENTRY_KEYS[bucket])!r}"
                )
                continue
            name = entry["name"]
            if type(name) is not str or not IMAGE_NAME_RE.fullmatch(name):
                errors.append(f"{bucket}[{index}].name is invalid")
                continue
            if bucket == "failed":
                status = entry["status"]
                if type(status) is not str or status not in FAILED_STATUSES:
                    errors.append(f"failed[{index}].status is invalid")
            if name in names:
                errors.append(f"{bucket} contains duplicate image: {name}")
            names.add(name)
            previous = all_names.get(name)
            if previous is not None and previous != bucket:
                errors.append(f"image appears in both {previous} and {bucket}: {name}")
            all_names[name] = bucket
        names_by_bucket[bucket] = names
    built = names_by_bucket.get("built", set())
    for name in sorted(expected - built):
        errors.append(f"built is missing planned image: {name}")
    for name in sorted(built - expected):
        errors.append(f"built contains unexpected image: {name}")
    for bucket in ("failed", "skipped", "unbuildable"):
        if names_by_bucket.get(bucket):
            errors.append(f"{bucket} must be empty")
    for name in sorted(expected & names_by_bucket.get("not_matched", set())):
        errors.append(f"planned image appears in not_matched: {name}")
    return errors


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--kolla-summary", required=True, type=Path)
    parser.add_argument("--publish-plan", required=True, type=Path)
    parser.add_argument("--arch", required=True, choices=("amd64", "arm64"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        plan = load_json(args.publish_plan)
        if type(plan) is not dict:
            raise ValueError("frozen publish plan must be an object")
        expected, planned_summary_path = planned_build(plan, args.arch)
        if args.kolla_summary != planned_summary_path:
            raise ValueError(
                "Kolla summary path does not match the frozen command: "
                f"{args.kolla_summary} != {planned_summary_path}"
            )
        summary = load_json(args.kolla_summary)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Invalid Kolla build summary input: {exc}", file=sys.stderr)
        return 2

    errors = validate_summary(summary, expected)
    if errors:
        print("Kolla build summary validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("Kolla build summary validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
