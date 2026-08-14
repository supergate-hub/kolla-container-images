#!/usr/bin/env python3
"""Protect append-only source-set history and release branch projections."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


BASELINE_RE = re.compile(r"^[0-9a-f]{40}$")
BRANCH_RE = re.compile(r"^(?:main|[0-9]{4}-[0-9]+)$")
RELEASE_RE = re.compile(r"^[0-9]{4}\.[0-9]+$")
SOURCE_SET_FILENAME_RE = re.compile(r"^[a-z][a-z0-9-]*\.json$")
ZERO_BASELINE = "0" * 40
SOURCE_SET_PREFIX = "config/openstack-sources/"


class HistoryValidationError(ValueError):
    """Raised when immutable source history cannot be proven."""


@dataclass(frozen=True)
class BaselineSourceSet:
    path: str
    content: bytes
    release: str


def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise HistoryValidationError(f"duplicate JSON key: {key!r}")
        value[key] = item
    return value


def parse_json_bytes(content: bytes, *, context: str) -> Any:
    try:
        return json.loads(
            content.decode("utf-8"),
            object_pairs_hook=reject_duplicate_keys,
        )
    except (HistoryValidationError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HistoryValidationError(f"cannot parse {context}: {error}") from error


def load_json(path: Path, *, root: Path) -> Any:
    try:
        content = path.read_bytes()
    except OSError as error:
        try:
            label = path.relative_to(root).as_posix()
        except ValueError:
            label = str(path)
        raise HistoryValidationError(f"cannot read {label}: {error}") from error
    return parse_json_bytes(content, context=path.relative_to(root).as_posix())


def run_git(root: Path, arguments: list[str]) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            ["git", *arguments],
            cwd=root,
            check=False,
            capture_output=True,
        )
    except OSError as error:
        raise HistoryValidationError(f"cannot execute Git: {error}") from error


def repository_root() -> Path:
    cwd = Path.cwd().resolve()
    result = run_git(cwd, ["rev-parse", "--show-toplevel"])
    if result.returncode != 0:
        raise HistoryValidationError("current directory is not inside a Git repository")
    try:
        git_root = Path(result.stdout.decode("utf-8").strip()).resolve()
    except UnicodeDecodeError as error:
        raise HistoryValidationError("Git repository root is not valid UTF-8") from error
    if git_root != cwd:
        raise HistoryValidationError(
            "validator must run from the Git repository root"
        )
    return git_root


def normalize_baseline(value: str | None) -> tuple[str | None, str | None]:
    if value is None or value == "":
        return None, "not supplied"
    if value == ZERO_BASELINE:
        raise HistoryValidationError(
            "zero baseline must be resolved by CI to a trusted default-branch commit"
        )
    if not BASELINE_RE.fullmatch(value):
        raise HistoryValidationError(
            "baseline must be exactly 40 lowercase hex characters"
        )
    return value, None


def require_commit(root: Path, baseline: str) -> None:
    result = run_git(root, ["cat-file", "-t", baseline])
    if result.returncode != 0 or result.stdout != b"commit\n":
        raise HistoryValidationError(
            f"baseline commit is not available in this checkout: {baseline}"
        )


def baseline_source_sets(root: Path, baseline: str) -> list[BaselineSourceSet]:
    require_commit(root, baseline)
    result = run_git(
        root,
        [
            "ls-tree",
            "-r",
            "-z",
            "--full-tree",
            baseline,
            "--",
            SOURCE_SET_PREFIX,
        ],
    )
    if result.returncode != 0:
        raise HistoryValidationError(
            f"cannot inspect source-set tree at baseline {baseline}"
        )

    source_sets: list[BaselineSourceSet] = []
    for entry in result.stdout.split(b"\0"):
        if not entry:
            continue
        try:
            metadata, raw_path = entry.split(b"\t", 1)
            mode, object_type, object_id = metadata.decode("ascii").split(" ", 2)
            path = raw_path.decode("utf-8")
        except (UnicodeDecodeError, ValueError) as error:
            raise HistoryValidationError(
                "cannot safely decode a source-set path in the baseline tree"
            ) from error

        if not path.startswith(SOURCE_SET_PREFIX):
            raise HistoryValidationError(
                f"unsafe source-set path in baseline: {path!r}"
            )
        filename = path.removeprefix(SOURCE_SET_PREFIX)
        if (
            "/" in filename
            or not SOURCE_SET_FILENAME_RE.fullmatch(filename)
            or object_type != "blob"
            or mode not in {"100644", "100755"}
            or not re.fullmatch(r"[0-9a-f]{40,64}", object_id)
        ):
            raise HistoryValidationError(
                f"unsafe source-set path in baseline: {path!r}"
            )

        blob = run_git(root, ["cat-file", "blob", object_id])
        if blob.returncode != 0:
            raise HistoryValidationError(
                f"cannot read source-set blob from baseline: {path}"
            )
        document = parse_json_bytes(blob.stdout, context=f"baseline {path}")
        if not isinstance(document, dict):
            raise HistoryValidationError(
                f"baseline source-set must contain an object: {path}"
            )
        release = document.get("release")
        if not isinstance(release, str) or not RELEASE_RE.fullmatch(release):
            raise HistoryValidationError(
                f"baseline source-set has an invalid release: {path}"
            )
        source_sets.append(
            BaselineSourceSet(path=path, content=blob.stdout, release=release)
        )
    return source_sets


def catalog_contract(root: Path) -> tuple[set[str], list[str]]:
    matrix = load_json(root / "config" / "build-matrix.json", root=root)
    if not isinstance(matrix, dict):
        raise HistoryValidationError("config/build-matrix.json must contain an object")

    releases = matrix.get("releases")
    if (
        not isinstance(releases, dict)
        or not releases
        or any(not isinstance(release, str) or not RELEASE_RE.fullmatch(release) for release in releases)
    ):
        raise HistoryValidationError("matrix releases must be a non-empty release object")
    active_releases = set(releases)

    streams = matrix.get("streams")
    if not isinstance(streams, list):
        raise HistoryValidationError("matrix streams must be a list")
    stream_ids: list[str] = []
    for index, stream in enumerate(streams):
        if not isinstance(stream, dict) or not isinstance(stream.get("id"), str):
            raise HistoryValidationError(
                f"matrix streams[{index}] must contain a string id"
            )
        stream_ids.append(stream["id"])
    if len(stream_ids) != len(set(stream_ids)):
        raise HistoryValidationError("matrix stream ids must not contain duplicates")

    profile_names = matrix.get("profiles")
    if (
        not isinstance(profile_names, list)
        or not profile_names
        or any(not isinstance(name, str) or not name for name in profile_names)
        or len(profile_names) != len(set(profile_names))
    ):
        raise HistoryValidationError("matrix profiles must be unique non-empty strings")

    common_reviewed: list[str] | None = None
    common_profile = ""
    for profile_name in profile_names:
        if not re.fullmatch(r"[a-z][a-z0-9-]*", profile_name):
            raise HistoryValidationError(f"unsafe profile name: {profile_name!r}")
        profile = load_json(
            root / "config" / "profiles" / f"{profile_name}.json",
            root=root,
        )
        reviewed = profile.get("reviewed_streams") if isinstance(profile, dict) else None
        if (
            not isinstance(reviewed, list)
            or not reviewed
            or any(not isinstance(stream_id, str) or not stream_id for stream_id in reviewed)
            or len(reviewed) != len(set(reviewed))
        ):
            raise HistoryValidationError(
                f"profile {profile_name!r} reviewed_streams must be unique non-empty strings"
            )
        if common_reviewed is None:
            common_reviewed = reviewed
            common_profile = profile_name
        elif reviewed != common_reviewed:
            raise HistoryValidationError(
                "shared profiles must have identical reviewed_streams: "
                f"{common_profile!r} and {profile_name!r} differ"
            )

    assert common_reviewed is not None
    if len(active_releases) == 1:
        release = next(iter(active_releases))
        expected = [
            stream_id
            for stream_id in common_reviewed
            if stream_id.startswith(f"{release}-")
        ]
        if not expected:
            raise HistoryValidationError(
                f"shared profiles have no reviewed streams for release {release}"
            )
        if stream_ids != expected:
            missing = [stream_id for stream_id in expected if stream_id not in stream_ids]
            extra = [stream_id for stream_id in stream_ids if stream_id not in expected]
            raise HistoryValidationError(
                "release-local stream projection must exactly equal the release-prefixed "
                f"shared reviewed_streams subset for {release}; missing={missing!r}, "
                f"extra={extra!r}, expected_order={expected!r}"
            )

    return active_releases, stream_ids


def validate_history(
    root: Path,
    baseline: str,
    active_releases: set[str],
    branch: str,
) -> None:
    projection_release: str | None = None
    if branch and branch != "main":
        projection_release = branch.replace("-", ".", 1)
        if active_releases != {projection_release}:
            raise HistoryValidationError(
                f"release branch {branch!r} must own exactly {projection_release!r}"
            )
    for source_set in baseline_source_sets(root, baseline):
        if (
            projection_release is not None
            and source_set.release != projection_release
        ):
            # This is the deliberate aggregate-main -> release-branch projection
            # exception. Main and generic validation protect every baseline release.
            continue
        current_path = root / source_set.path
        if not current_path.exists():
            raise HistoryValidationError(
                f"owned source-set must not be deleted: {source_set.path}"
            )
        if current_path.is_symlink() or not current_path.is_file():
            raise HistoryValidationError(
                f"owned source-set must remain a regular file: {source_set.path}"
            )
        try:
            current_content = current_path.read_bytes()
        except OSError as error:
            raise HistoryValidationError(
                f"cannot read owned source-set {source_set.path}: {error}"
            ) from error
        if current_content != source_set.content:
            raise HistoryValidationError(
                f"owned source-set must remain byte-identical to {baseline}: "
                f"{source_set.path}"
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate append-only OpenStack source-set history.",
        allow_abbrev=False,
    )
    parser.add_argument(
        "--baseline",
        help="Exact 40-character Git commit SHA to compare against",
    )
    parser.add_argument(
        "--branch",
        default="",
        help="Validation branch context (main or exact YYYY-N)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.branch and not BRANCH_RE.fullmatch(args.branch):
            raise HistoryValidationError(
                "branch must be main or an exact YYYY-N name"
            )
        baseline, skip_reason = normalize_baseline(args.baseline)
        root = repository_root()
        active_releases, _ = catalog_contract(root)
        if baseline is None:
            print(
                "Source-set history validation passed; immutable Git history check "
                f"skipped ({skip_reason})."
            )
            return 0
        validate_history(root, baseline, active_releases, args.branch)
    except HistoryValidationError as error:
        print(f"Source-set history validation failed: {error}", file=sys.stderr)
        return 1

    print(f"Source-set history validation passed against {baseline}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
