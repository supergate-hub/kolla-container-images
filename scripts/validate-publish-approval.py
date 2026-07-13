#!/usr/bin/env python3
"""Validate a real GHCR publish approval against its frozen publish plan."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from profile_resolver import find_stream, load_matrix, validate_candidate_id
from publish_approval import ApprovalRequirement, approval_requirement


ROOT = Path(__file__).resolve().parents[1]
PLAN_PUBLISH = ROOT / "scripts" / "plan-publish.py"
EXPECTED_ARCHITECTURES = ["amd64", "arm64"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--publish-plan", required=True, type=Path)
    parser.add_argument("--expected-candidate-id", required=True)
    return parser.parse_args()


def reject(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


def plan_mismatch(field: str) -> ValueError:
    return ValueError(
        "Frozen publish plan does not exactly match repository planner output: "
        + field
    )


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def required_string(plan: dict[str, Any], field: str) -> str:
    value = plan.get(field)
    if type(value) is not str:
        raise plan_mismatch(field)
    return value


def planner_inputs(plan: dict[str, Any]) -> tuple[str, str, str | None, str]:
    stream = required_string(plan, "stream")
    profile = required_string(plan, "profile")
    candidate_id = required_string(plan, "candidate_id")
    if "image_filter" not in plan:
        raise plan_mismatch("image_filter")
    image_filter = plan["image_filter"]
    if image_filter is not None and type(image_filter) is not str:
        raise plan_mismatch("image_filter")
    return stream, profile, image_filter, candidate_id


def render_expected_plan(
    stream: str,
    profile: str,
    image_filter: str | None,
    candidate_id: str,
) -> dict[str, Any]:
    command = [
        sys.executable,
        str(PLAN_PUBLISH),
        "--stream",
        stream,
        "--profile",
        profile,
        "--candidate-id",
        candidate_id,
        "--dry-run",
    ]
    if image_filter is not None:
        command.extend(["--image", image_filter])
    result = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or "planner failed without an error message"
        raise ValueError(f"Unable to recompute publish plan: {detail}")
    expected = json.loads(result.stdout)
    if not isinstance(expected, dict):
        raise ValueError("Recomputed publish plan root is not a JSON object")
    return expected


def recompute_requirement(
    plan: dict[str, Any],
    expected_candidate_id: str,
) -> ApprovalRequirement | None:
    expected_candidate_id = validate_candidate_id(
        expected_candidate_id,
        allow_local=False,
    )
    stream_id, profile_name, image_filter, candidate_id = planner_inputs(plan)
    if candidate_id != expected_candidate_id:
        raise ValueError(
            "Frozen publish plan candidate ID does not match trusted workflow context"
        )
    matrix = load_matrix()
    if canonical_json(matrix.get("architectures")) != canonical_json(
        EXPECTED_ARCHITECTURES
    ):
        raise ValueError("Repository architectures must be exactly amd64 and arm64")
    stream = find_stream(matrix, stream_id)
    if stream.get("publish_enabled") is not True:
        raise ValueError(f"Stream {stream_id} is not enabled for publication")

    expected_plan = render_expected_plan(
        stream_id,
        profile_name,
        image_filter,
        candidate_id,
    )
    if canonical_json(plan) != canonical_json(expected_plan):
        raise plan_mismatch("complete plan")

    scope = expected_plan["scope"]
    registry_path = (
        f"{expected_plan['registry']}/{expected_plan['owner']}/"
        f"{expected_plan['repository']}"
    )
    return approval_requirement(
        registry_path,
        expected_plan["stream"],
        scope["profile"],
        scope["image"],
        scope["image_count"],
    )


def load_publish_plan(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as file_obj:
        plan = json.load(file_obj)
    if not isinstance(plan, dict):
        raise ValueError("publish plan root must be a JSON object")
    return plan


def main() -> int:
    args = parse_args()
    try:
        plan = load_publish_plan(args.publish_plan)
        requirement = recompute_requirement(plan, args.expected_candidate_id)
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        return reject(f"Invalid publish plan: {exc}")

    if requirement is None:
        scope = plan["scope"]
        return reject(
            "Requested scope is not approved for real publish: "
            f"{scope['profile']}/{scope['image']} {plan['stream']}."
        )
    if os.environ.get(requirement.variable) != "true":
        return reject(f"Publish requires {requirement.variable}=true.")
    if os.environ.get("APPROVAL", "") != requirement.phrase:
        return reject("Publish requires the exact approval phrase from the frozen plan.")

    print("Publish approval validated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
