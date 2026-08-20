#!/usr/bin/env python3
"""Fail-closed protected-main validation for publication workflows."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from release_policy import validate_publish_context


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
        description="Validate a frozen publish plan against protected main.",
        allow_abbrev=False,
    )
    parser.add_argument("--matrix", required=True, type=Path)
    parser.add_argument("--publish-plan", required=True, type=Path)
    parser.add_argument("--git-ref", required=True)
    parser.add_argument("--require-protected", action="store_true")
    parser.add_argument(
        "--ref-protected",
        choices=("true", "false"),
        help="Exact GitHub ref protection state; required with --require-protected",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        matrix = load_json(args.matrix, "matrix")
        if args.require_protected and args.ref_protected is None:
            raise ValueError("--ref-protected is required with --require-protected")
        if not args.require_protected and args.ref_protected is not None:
            raise ValueError("--ref-protected is only valid with --require-protected")
        plan = load_json(args.publish_plan, "publish plan")
        errors = validate_publish_context(
            matrix,
            plan,
            args.git_ref,
            require_protected=args.require_protected,
            ref_protected=(args.ref_protected == "true"),
        )
    except ValueError as error:
        print(f"Publication context validation failed:\n- {error}", file=sys.stderr)
        return 1

    if errors:
        print("Publication context validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("Publication context validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
