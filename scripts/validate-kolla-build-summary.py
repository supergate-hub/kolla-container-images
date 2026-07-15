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


UNIT_KEYS = {
    "id",
    "kind",
    "tier",
    "arch",
    "runner",
    "runner_machine",
    "kolla_base_arch",
    "platform",
    "target",
    "ancestor_chain",
    "ancestors",
    "arch_ref",
    "summary_file",
    "logs_dir",
    "command",
}


def planned_units(plan: dict[str, Any]) -> list[dict[str, Any]]:
    build = plan.get("build")
    if type(build) is not dict:
        raise ValueError("frozen plan build must be an object")
    units = build.get("all_units")
    if type(units) is not list:
        raise ValueError("frozen plan build all_units must be a list")
    result: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, unit in enumerate(units):
        if type(unit) is not dict or set(unit) != UNIT_KEYS:
            raise ValueError(f"frozen build unit {index} schema is invalid")
        unit_id = unit.get("id")
        if type(unit_id) is not str or not unit_id or unit_id in seen_ids:
            raise ValueError("frozen build unit IDs must be non-empty and unique")
        seen_ids.add(unit_id)
        result.append(unit)
    return result


def planned_unit(plan: dict[str, Any], unit_id: str) -> tuple[set[str], set[str], Path]:
    matches = [unit for unit in planned_units(plan) if unit["id"] == unit_id]
    if len(matches) != 1:
        raise ValueError(f"frozen plan must contain exactly one build unit: {unit_id}")
    unit = matches[0]
    target = unit.get("target")
    ancestor_chain = unit.get("ancestor_chain")
    ancestors = unit.get("ancestors")
    if type(target) is not str or not IMAGE_NAME_RE.fullmatch(target):
        raise ValueError(f"frozen build unit target is invalid: {unit_id}")
    if (
        type(ancestor_chain) is not list
        or not all(type(name) is str and IMAGE_NAME_RE.fullmatch(name) for name in ancestor_chain)
        or len(ancestor_chain) != len(set(ancestor_chain))
    ):
        raise ValueError(f"frozen build unit ancestor_chain is invalid: {unit_id}")
    if type(ancestors) is not list or any(
        type(entry) is not dict
        or set(entry) != {"image", "arch_ref"}
        or type(entry["image"]) is not str
        or type(entry["arch_ref"]) is not str
        for entry in ancestors
    ):
        raise ValueError(f"frozen build unit ancestors are invalid: {unit_id}")
    if [entry["image"] for entry in ancestors] != ancestor_chain:
        raise ValueError(
            f"frozen build unit ancestors must match ancestor_chain: {unit_id}"
        )
    summary_file = unit.get("summary_file")
    if type(summary_file) is not str or not summary_file:
        raise ValueError(f"frozen build unit summary_file is invalid: {unit_id}")
    command = unit.get("command")
    if type(command) is not list or not command or not all(type(part) is str for part in command):
        raise ValueError(f"frozen build unit command must be a string argv list: {unit_id}")
    positions = [index for index, part in enumerate(command) if part == "--summary-json-file"]
    if (
        len(positions) != 1
        or positions[0] + 1 >= len(command)
        or command[positions[0] + 1] != summary_file
    ):
        raise ValueError(
            f"frozen build unit command summary path is invalid: {unit_id}"
        )
    return {target}, set(ancestor_chain), Path(summary_file)


def validate_summary(
    summary: Any,
    expected_built: set[str],
    expected_skipped: set[str],
) -> list[str]:
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
    skipped = names_by_bucket.get("skipped", set())
    for name in sorted(expected_built - built):
        errors.append(f"built is missing planned image: {name}")
    for name in sorted(built - expected_built):
        errors.append(f"built contains unexpected image: {name}")
    for name in sorted(expected_skipped - skipped):
        errors.append(f"skipped is missing planned ancestor: {name}")
    for name in sorted(skipped - expected_skipped):
        errors.append(f"skipped contains unexpected image: {name}")
    if names_by_bucket.get("failed"):
        errors.append("failed must be empty")
    planned_names = expected_built | expected_skipped
    for bucket in ("not_matched", "unbuildable"):
        for name in sorted(planned_names & names_by_bucket.get(bucket, set())):
            errors.append(f"planned image appears in {bucket}: {name}")
    return errors


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--kolla-summary", required=True, type=Path)
    parser.add_argument("--publish-plan", required=True, type=Path)
    parser.add_argument("--unit-id", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        plan = load_json(args.publish_plan)
        if type(plan) is not dict:
            raise ValueError("frozen publish plan must be an object")
        expected_built, expected_skipped, planned_summary_path = planned_unit(
            plan, args.unit_id
        )
        if args.kolla_summary != planned_summary_path:
            raise ValueError(
                "Kolla summary path does not match the frozen command: "
                f"{args.kolla_summary} != {planned_summary_path}"
            )
        summary = load_json(args.kolla_summary)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Invalid Kolla build summary input: {exc}", file=sys.stderr)
        return 2

    errors = validate_summary(summary, expected_built, expected_skipped)
    if errors:
        print("Kolla build summary validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("Kolla build summary validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
