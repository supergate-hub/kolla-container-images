#!/usr/bin/env python3
"""Create or refresh the generated publish-dropdown PR above a matrix PR.

This command is intentionally run only from a trusted checkout of ``main``.
Proposal files are fetched as raw data and are never executed.
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
PULL_REQUEST_RE = re.compile(r"^[1-9][0-9]*$")
BOT_BRANCH_PREFIX = "automation/sync-publish-stream-options/pr-"
WORKFLOW_PATH = ".github/workflows/publish.yml"
MATRIX_PATH = "config/build-matrix.json"


class StackSyncError(RuntimeError):
    """Raised when a generated dropdown stack PR cannot be safely updated."""


@dataclass(frozen=True)
class StackRequest:
    repository: str
    head_sha: str
    source_branch: str
    pull_request_number: str
    repository_dir: Path
    app_token: str

    @property
    def bot_branch(self) -> str:
        return f"{BOT_BRANCH_PREFIX}{self.pull_request_number}"


def redact(value: str, secrets: tuple[str, ...]) -> str:
    for secret in secrets:
        if secret:
            value = value.replace(secret, "***")
    return value


def run_command(
    arguments: list[str],
    *,
    cwd: Path | None = None,
    secrets: tuple[str, ...] = (),
) -> str:
    """Run a command, returning stdout and never echoing supplied secrets."""
    result = subprocess.run(
        arguments,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        command = redact(shlex.join(arguments), secrets)
        stderr = redact(result.stderr.strip(), secrets)
        raise StackSyncError(
            f"command failed with exit code {result.returncode}: {command}"
            + (f"\n{stderr}" if stderr else "")
        )
    return result.stdout


def validate_request(request: StackRequest) -> None:
    if REPOSITORY_RE.fullmatch(request.repository) is None:
        raise StackSyncError("repository must be an exact owner/name value")
    if SHA_RE.fullmatch(request.head_sha) is None:
        raise StackSyncError("pull request head must be an exact lowercase SHA")
    if PULL_REQUEST_RE.fullmatch(request.pull_request_number) is None:
        raise StackSyncError("pull request number must be a positive integer")
    if not request.source_branch or "\n" in request.source_branch:
        raise StackSyncError("pull request source branch must be a single line")
    if not request.repository_dir.is_dir():
        raise StackSyncError(f"trusted repository does not exist: {request.repository_dir}")
    if not request.app_token:
        raise StackSyncError("GitHub App token is required")


def proposal_content(request: StackRequest, path: str) -> str:
    return run_command(
        [
            "gh",
            "api",
            "--method",
            "GET",
            "-H",
            "Accept: application/vnd.github.raw",
            f"/repos/{request.repository}/contents/{path}?ref={request.head_sha}",
        ]
    )


def render_dropdown(
    request: StackRequest,
    matrix_path: Path,
    workflow_path: Path,
) -> None:
    synchronizer = request.repository_dir / "scripts" / "sync-publish-stream-options.py"
    if not synchronizer.is_file():
        raise StackSyncError(f"trusted synchronizer does not exist: {synchronizer}")
    run_command(
        [
            sys.executable,
            str(synchronizer),
            "--matrix",
            str(matrix_path),
            "--workflow",
            str(workflow_path),
            "--write",
        ]
    )


def existing_stack_pull_request(request: StackRequest) -> str | None:
    number = run_command(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            request.repository,
            "--head",
            request.bot_branch,
            "--state",
            "open",
            "--json",
            "number",
            "--jq",
            ".[0].number // empty",
        ]
    ).strip()
    if not number:
        return None
    if PULL_REQUEST_RE.fullmatch(number) is None:
        raise StackSyncError("GitHub returned an invalid existing pull request number")
    return number


def close_redundant_stack_pull_request(number: str, request: StackRequest) -> None:
    run_command(
        [
            "gh",
            "pr",
            "close",
            number,
            "--repo",
            request.repository,
            "--delete-branch",
            "--comment",
            (
                "Dropdown is already synchronized by "
                f"PR #{request.pull_request_number}."
            ),
        ]
    )


def git(request: StackRequest, *arguments: str, secret: bool = False) -> str:
    return run_command(
        ["git", "-C", str(request.repository_dir), *arguments],
        secrets=(request.app_token,) if secret else (),
    )


def create_stack_commit(request: StackRequest, rendered_workflow: Path) -> None:
    remote_url = (
        "https://x-access-token:"
        f"{request.app_token}@github.com/{request.repository}.git"
    )
    git(request, "remote", "set-url", "origin", remote_url, secret=True)
    git(
        request,
        "fetch",
        "--no-tags",
        "origin",
        f"refs/pull/{request.pull_request_number}/head",
        secret=True,
    )
    fetched_head = git(request, "rev-parse", "FETCH_HEAD").strip()
    if fetched_head != request.head_sha:
        raise StackSyncError("fetched pull request head does not match the event SHA")
    git(request, "-c", "core.hooksPath=/dev/null", "checkout", "--detach", request.head_sha)

    target_workflow = request.repository_dir / WORKFLOW_PATH
    target_workflow.write_text(
        rendered_workflow.read_text(encoding="utf-8"), encoding="utf-8"
    )
    git(request, "add", WORKFLOW_PATH)
    git(
        request,
        "-c",
        "user.name=Kolla Catalog Bot",
        "-c",
        "user.email=kolla-catalog-bot@users.noreply.github.com",
        "-c",
        "core.hooksPath=/dev/null",
        "commit",
        "-m",
        f"chore: sync publish stream dropdown for #{request.pull_request_number}",
    )

    remote_tip = run_command(
        [
            "git",
            "ls-remote",
            "origin",
            f"refs/heads/{request.bot_branch}",
        ],
        cwd=request.repository_dir,
        secrets=(request.app_token,),
    ).split(maxsplit=1)
    if remote_tip:
        git(
            request,
            "push",
            f"--force-with-lease=refs/heads/{request.bot_branch}:{remote_tip[0]}",
            "origin",
            f"HEAD:refs/heads/{request.bot_branch}",
            secret=True,
        )
    else:
        git(
            request,
            "push",
            "origin",
            f"HEAD:refs/heads/{request.bot_branch}",
            secret=True,
        )


def create_or_update_stack_pull_request(
    existing_number: str | None,
    request: StackRequest,
) -> None:
    title = f"chore: sync publish stream dropdown for #{request.pull_request_number}"
    body = (
        f"Generated from #{request.pull_request_number} by the trusted main "
        "catalog synchronizer. Merge this top stack PR after validation to land "
        "both changes."
    )
    if existing_number is None:
        run_command(
            [
                "gh",
                "pr",
                "create",
                "--repo",
                request.repository,
                "--base",
                request.source_branch,
                "--head",
                request.bot_branch,
                "--title",
                title,
                "--body",
                body,
            ]
        )
        return
    run_command(
        [
            "gh",
            "pr",
            "edit",
            existing_number,
            "--repo",
            request.repository,
            "--title",
            title,
            "--body",
            body,
        ]
    )


def synchronize_stack_pull_request(request: StackRequest) -> bool:
    """Return whether a stack PR was created or refreshed."""
    validate_request(request)
    with tempfile.TemporaryDirectory(prefix="publish-stream-options-") as temp_dir:
        temp_path = Path(temp_dir)
        matrix_path = temp_path / "build-matrix.json"
        workflow_path = temp_path / "publish.yml"
        matrix_path.write_text(proposal_content(request, MATRIX_PATH), encoding="utf-8")
        workflow_path.write_text(
            proposal_content(request, WORKFLOW_PATH), encoding="utf-8"
        )
        original_workflow = workflow_path.read_text(encoding="utf-8")
        render_dropdown(request, matrix_path, workflow_path)
        existing_number = existing_stack_pull_request(request)
        if workflow_path.read_text(encoding="utf-8") == original_workflow:
            if existing_number is not None:
                close_redundant_stack_pull_request(existing_number, request)
            return False
        create_stack_commit(request, workflow_path)
        create_or_update_stack_pull_request(existing_number, request)
        return True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--source-branch", required=True)
    parser.add_argument("--pull-request-number", required=True)
    parser.add_argument("--repository-dir", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        created = synchronize_stack_pull_request(
            StackRequest(
                repository=args.repository,
                head_sha=args.head_sha,
                source_branch=args.source_branch,
                pull_request_number=args.pull_request_number,
                repository_dir=args.repository_dir.resolve(),
                app_token=os.environ.get("APP_TOKEN", ""),
            )
        )
    except StackSyncError as error:
        print(f"Publish stream dropdown synchronization failed: {error}", file=sys.stderr)
        return 1
    if created:
        print("Created or refreshed the publish stream dropdown stack PR.")
    else:
        print("Publish stream dropdown is already synchronized.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
