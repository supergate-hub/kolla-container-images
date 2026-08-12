#!/usr/bin/env python3
"""Fail-closed release branch validation for CI and publication workflows."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from release_policy import validate_matrix_branch, validate_publish_context


def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as file_obj:
            value = json.load(file_obj, object_pairs_hook=reject_duplicate_keys)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot load {label} {path}: {error}") from error
    if type(value) is not dict:
        raise ValueError(f"{label} must be a JSON object")
    return value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate release-branch ownership for a matrix or publish plan.",
        allow_abbrev=False,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    matrix_parser = subparsers.add_parser(
        "matrix", help="Validate a branch-local build matrix"
    )
    matrix_parser.add_argument("--matrix", required=True, type=Path)
    matrix_parser.add_argument("--branch", required=True)

    publish_parser = subparsers.add_parser(
        "publish", help="Bind a frozen publish plan to its exact release ref"
    )
    publish_parser.add_argument("--matrix", required=True, type=Path)
    publish_parser.add_argument("--publish-plan", required=True, type=Path)
    publish_parser.add_argument("--git-ref", required=True)
    publish_parser.add_argument("--require-protected", action="store_true")
    publish_parser.add_argument(
        "--ref-protected",
        choices=("true", "false"),
        help="Exact GitHub ref protection state; required with --require-protected",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        matrix = load_json(args.matrix, "matrix")
        if args.command == "matrix":
            errors = validate_matrix_branch(matrix, args.branch)
        else:
            if args.require_protected and args.ref_protected is None:
                raise ValueError(
                    "--ref-protected is required with --require-protected"
                )
            if not args.require_protected and args.ref_protected is not None:
                raise ValueError(
                    "--ref-protected is only valid with --require-protected"
                )
            plan = load_json(args.publish_plan, "publish plan")
            errors = validate_publish_context(
                matrix,
                plan,
                args.git_ref,
                require_protected=args.require_protected,
                ref_protected=(args.ref_protected == "true"),
            )
    except ValueError as error:
        print(f"Release context validation failed:\n- {error}", file=sys.stderr)
        return 1

    if errors:
        print("Release context validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("Release context validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
