"""Bind reruns to their plan producer and locate immutable unit checkpoints."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Any

try:
    from scripts.network_retry import run_network_command
except ModuleNotFoundError:
    from network_retry import run_network_command

POSITIVE_INTEGER = re.compile(r"[1-9][0-9]*")


def candidate_for_attempt(candidate_id: str, run_id: str, run_attempt: str) -> str:
    """A retry may consume an earlier plan, but never another run or future plan."""
    if not all(isinstance(value, str) and POSITIVE_INTEGER.fullmatch(value)
               for value in (run_id, run_attempt)):
        raise ValueError("workflow run and attempt must be positive canonical integers")
    if not isinstance(candidate_id, str) or not re.fullmatch(r"[1-9][0-9]*-[1-9][0-9]*", candidate_id):
        raise ValueError("invalid plan producer candidate ID")
    producer_run, producer_attempt = candidate_id.split("-")
    if producer_run != run_id or int(producer_attempt) > int(run_attempt):
        raise ValueError("candidate must come from this run and a current or earlier plan attempt")
    return candidate_id


def find_unit_artifact(pages: Any, name: str) -> str | None:
    if not isinstance(pages, list):
        raise ValueError("artifact response must be a page list")
    matches = []
    for page in pages:
        if not isinstance(page, dict) or not isinstance(page.get("artifacts"), list):
            raise ValueError("invalid workflow artifact page")
        for artifact in page["artifacts"]:
            if not isinstance(artifact, dict):
                raise ValueError("invalid workflow artifact entry")
            if artifact.get("name") == name:
                matches.append(artifact)
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError("ambiguous unit checkpoint")
    artifact = matches[0]
    if artifact.get("expired") is not False:
        raise ValueError("unit checkpoint expired; use Re-run all jobs")
    identifier = artifact.get("id")
    if type(identifier) is not int or identifier <= 0:
        raise ValueError("invalid unit checkpoint ID")
    return str(identifier)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-attempt", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--unit-id", required=True)
    parser.add_argument("--github-output", required=True, type=Path)
    args = parser.parse_args(argv)
    candidate_for_attempt(args.candidate_id, args.run_id, args.run_attempt)
    if args.repository != "supergate-hub/kolla-container-images":
        raise ValueError("unit checkpoint must belong to this repository")
    if not re.fullmatch(r"(?:amd64|arm64)-(?:parent|leaf)-[a-z0-9-]+", args.unit_id):
        raise ValueError("invalid build unit identity")
    artifact_id = None
    # A new plan has no completed units. Avoid adding an API request to every
    # first-attempt build; checkpoints are relevant only when resuming it.
    if int(args.run_attempt) > int(args.candidate_id.split("-")[1]):
        result = run_network_command([
            "gh", "api", "--paginate", "--slurp",
            f"/repos/{args.repository}/actions/runs/{args.run_id}/artifacts?per_page=100",
        ], label="GitHub unit checkpoint lookup", timeout=60)
        name = f"unit-evidence-{args.unit_id}-{args.candidate_id}"
        artifact_id = find_unit_artifact(json.loads(result.stdout), name)
    with args.github_output.open("a", encoding="utf-8") as output:
        output.write(f"found={'true' if artifact_id else 'false'}\n")
        if artifact_id:
            output.write(f"artifact_id={artifact_id}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
