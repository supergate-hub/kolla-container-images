#!/usr/bin/env python3
"""Synchronize the static Actions stream dropdown with the build matrix."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MATRIX = ROOT / "config" / "build-matrix.json"
DEFAULT_WORKFLOW = ROOT / ".github" / "workflows" / "publish.yml"
BEGIN_MARKER = "# BEGIN GENERATED STREAM OPTIONS"
END_MARKER = "# END GENERATED STREAM OPTIONS"


def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key!r}")
        value[key] = item
    return value


def enabled_stream_ids(matrix_path: Path) -> list[str]:
    try:
        matrix = json.loads(
            matrix_path.read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicate_keys,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"cannot load build matrix {matrix_path}: {error}") from error
    streams = matrix.get("streams") if isinstance(matrix, dict) else None
    if not isinstance(streams, list):
        raise ValueError("matrix streams must be a list")

    stream_ids: list[str] = []
    for index, stream in enumerate(streams):
        if not isinstance(stream, dict):
            raise ValueError(f"matrix streams[{index}] must be an object")
        stream_id = stream.get("id")
        if not isinstance(stream_id, str) or not stream_id:
            raise ValueError(f"matrix streams[{index}].id must be a non-empty string")
        if stream.get("publish_enabled") is True:
            stream_ids.append(stream_id)
    if not stream_ids:
        raise ValueError("matrix must contain at least one enabled stream")
    if len(stream_ids) != len(set(stream_ids)):
        raise ValueError("enabled stream IDs must be unique")
    return stream_ids


def render_stream_options(workflow: str, stream_ids: list[str]) -> str:
    """Replace exactly one marked choice block while preserving all other YAML."""
    lines = workflow.splitlines(keepends=True)
    begin_indices = [
        index for index, line in enumerate(lines) if line.strip() == BEGIN_MARKER
    ]
    end_indices = [
        index for index, line in enumerate(lines) if line.strip() == END_MARKER
    ]
    if len(begin_indices) != 1 or len(end_indices) != 1:
        raise ValueError("workflow must contain exactly one generated stream-options block")
    begin, end = begin_indices[0], end_indices[0]
    if begin >= end:
        raise ValueError("generated stream-options markers are out of order")
    indentation = lines[begin][: len(lines[begin]) - len(lines[begin].lstrip())]
    if lines[end][: len(lines[end]) - len(lines[end].lstrip())] != indentation:
        raise ValueError("generated stream-options markers must use the same indentation")
    rendered = [
        f"{indentation}{BEGIN_MARKER}\n",
        *(f"{indentation}- {stream_id}\n" for stream_id in stream_ids),
        f"{indentation}{END_MARKER}\n",
    ]
    return "".join([*lines[:begin], *rendered, *lines[end + 1 :]])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--matrix", type=Path, default=DEFAULT_MATRIX)
    parser.add_argument("--workflow", type=Path, default=DEFAULT_WORKFLOW)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--check", action="store_true")
    action.add_argument("--write", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        stream_ids = enabled_stream_ids(args.matrix)
        workflow = args.workflow.read_text(encoding="utf-8")
        rendered = render_stream_options(workflow, stream_ids)
    except (OSError, UnicodeDecodeError, ValueError) as error:
        print(f"Publish stream options validation failed: {error}", file=sys.stderr)
        return 1

    if args.write:
        if rendered != workflow:
            args.workflow.write_text(rendered, encoding="utf-8")
            print(f"Updated stream options in {args.workflow}.")
        else:
            print("Publish stream options are already synchronized.")
        return 0
    if rendered != workflow:
        print(
            "Publish stream options are out of date; run "
            "scripts/sync-publish-stream-options.py --write.",
            file=sys.stderr,
        )
        return 1
    print("Publish stream options are synchronized.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
