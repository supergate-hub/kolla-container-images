#!/usr/bin/env python3
"""Select the terminal artifact of a completed Publish Kolla images workflow."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


class ArtifactSelectionError(ValueError):
    """Raised when a completed workflow has an ambiguous terminal artifact."""


def _artifact_pages(document: Any) -> list[dict[str, Any]]:
    if isinstance(document, dict):
        return [document]
    if isinstance(document, list) and all(isinstance(page, dict) for page in document):
        return document
    raise ArtifactSelectionError("workflow artifacts response must be an object or page list")


def select_terminal_artifact(
    document: Any,
    *,
    run_id: str,
    run_attempt: str,
) -> str | None:
    if not run_id.isdecimal() or not run_attempt.isdecimal():
        raise ArtifactSelectionError("run id and attempt must be decimal strings")
    suffix = f"-{run_id}-{run_attempt}"
    matches: list[str] = []
    for page in _artifact_pages(document):
        artifacts = page.get("artifacts")
        if not isinstance(artifacts, list):
            raise ArtifactSelectionError("workflow artifact page is missing artifacts")
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                raise ArtifactSelectionError("workflow artifact entry must be an object")
            name = artifact.get("name")
            expired = artifact.get("expired", False)
            if not isinstance(name, str) or not isinstance(expired, bool):
                raise ArtifactSelectionError("workflow artifact has invalid name or expiry")
            if (
                name.startswith("publish-")
                and not name.startswith("publish-plan-")
                and name.endswith(suffix)
            ):
                if expired:
                    raise ArtifactSelectionError("terminal publish artifact has expired")
                matches.append(name)
    if not matches:
        return None
    if len(matches) != 1:
        raise ArtifactSelectionError("workflow run has multiple terminal publish artifacts")
    return matches[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-attempt", required=True)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args(argv)
    document = json.loads(args.artifacts.read_text(encoding="utf-8"))
    artifact = select_terminal_artifact(
        document,
        run_id=args.run_id,
        run_attempt=args.run_attempt,
    )
    output = f"should_refresh={'true' if artifact else 'false'}\n"
    if artifact:
        output += f"artifact_name={artifact}\n"
    if args.github_output:
        args.github_output.write_text(output, encoding="utf-8")
    else:
        print(output, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
