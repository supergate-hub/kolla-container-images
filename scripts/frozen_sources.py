from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import unquote, urlparse

try:
    from scripts.profile_resolver import find_stream
except ModuleNotFoundError:
    from profile_resolver import find_stream


RELEASES_REPOSITORY = "https://opendev.org/openstack/releases"
PIN_KEYS = {"repository", "version", "commit"}
METADATA_PIN_KEYS = {"repository", "commit"}
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SERIES_RE = re.compile(r"^[a-z][a-z0-9-]*$")
VERSION_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)+(?:[A-Za-z0-9.-]*)?$")
DELIVERABLE_PROJECTS = {
    "kolla": "openstack/kolla",
    "kolla_ansible": "openstack/kolla-ansible",
}
DELIVERABLE_FILES = {
    "kolla": "kolla.yaml",
    "kolla_ansible": "kolla-ansible.yaml",
}


class FrozenSourceError(ValueError):
    """Raised when frozen source provenance cannot be proven exactly."""


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise FrozenSourceError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def load_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise FrozenSourceError(f"cannot read JSON object {path}: {error}") from error
    if not isinstance(value, dict):
        raise FrozenSourceError(f"JSON document must be an object: {path}")
    return value


def _require_exact_pin(
    value: Any,
    *,
    context: str,
    expected: dict[str, str],
    keys: set[str],
) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != keys:
        raise FrozenSourceError(
            f"{context} keys must be exactly {sorted(keys)!r}"
        )
    for key in keys:
        if value.get(key) != expected[key]:
            raise FrozenSourceError(
                f"{context}.{key} does not match the branch matrix pin"
            )
    commit = value["commit"]
    if not isinstance(commit, str) or not SHA_RE.fullmatch(commit):
        raise FrozenSourceError(
            f"{context}.commit must be a lowercase 40-character SHA"
        )
    return {key: value[key] for key in keys}


def validate_plan_source_pins(
    matrix: dict[str, Any], plan: dict[str, Any]
) -> dict[str, Any]:
    """Return the exact source contract after comparing plan and matrix pins."""
    stream_id = plan.get("stream")
    if not isinstance(stream_id, str):
        raise FrozenSourceError("publish plan stream must be a string")
    try:
        stream = find_stream(matrix, stream_id)
    except (KeyError, TypeError, ValueError) as error:
        raise FrozenSourceError(
            f"cannot resolve publish plan stream: {error}"
        ) from error

    for field in ("release", "release_series", "release_branch"):
        if plan.get(field) != stream[field]:
            raise FrozenSourceError(
                f"publish plan {field} does not match the branch matrix stream"
            )

    matrix_metadata = matrix.get("release_metadata")
    if (
        not isinstance(matrix_metadata, dict)
        or set(matrix_metadata) != METADATA_PIN_KEYS
        or matrix_metadata.get("repository") != RELEASES_REPOSITORY
    ):
        raise FrozenSourceError(
            "matrix release_metadata must contain only the canonical OpenStack "
            "Releases repository and an exact commit"
        )
    if not SHA_RE.fullmatch(str(matrix_metadata.get("commit", ""))):
        raise FrozenSourceError(
            "matrix release_metadata.commit must be a lowercase 40-character SHA"
        )

    release_metadata = _require_exact_pin(
        plan.get("release_metadata"),
        context="publish plan release_metadata",
        expected=matrix_metadata,
        keys=METADATA_PIN_KEYS,
    )
    sources: dict[str, dict[str, str]] = {}
    for project in DELIVERABLE_PROJECTS:
        expected = {
            "repository": stream[f"{project}_repository"],
            "version": stream[f"{project}_version"],
            "commit": stream[f"{project}_commit"],
        }
        sources[project] = _require_exact_pin(
            plan.get(project),
            context=f"publish plan {project}",
            expected=expected,
            keys=PIN_KEYS,
        )
        if not VERSION_RE.fullmatch(sources[project]["version"]):
            raise FrozenSourceError(
                f"publish plan {project}.version has an unsupported format"
            )

    for project in DELIVERABLE_PROJECTS:
        legacy_key = f"{project}_version"
        if legacy_key in plan and plan[legacy_key] != sources[project]["version"]:
            raise FrozenSourceError(
                f"publish plan {legacy_key} conflicts with its frozen source pin"
            )

    return {
        "stream": stream_id,
        "release": stream["release"],
        "series": stream["release_series"],
        "release_metadata": release_metadata,
        **sources,
    }


def parse_deliverable_pin(
    path: Path, *, expected_project: str, expected_version: str
) -> str:
    """Parse one pinned OpenStack Releases deliverable without network or YAML libs."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise FrozenSourceError(
            f"cannot read release metadata {path}: {error}"
        ) from error
    if "\t" in text:
        raise FrozenSourceError(f"release metadata contains a tab: {path}")
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise FrozenSourceError(f"release metadata must start with '---': {path}")
    release_markers = [index for index, line in enumerate(lines) if line == "releases:"]
    if len(release_markers) != 1:
        raise FrozenSourceError(
            f"release metadata must contain exactly one releases section: {path}"
        )

    start = release_markers[0] + 1
    section: list[str] = []
    for line in lines[start:]:
        if line and not line.startswith(" "):
            break
        section.append(line)
    if not section:
        raise FrozenSourceError(
            f"release metadata has an empty releases section: {path}"
        )

    version_re = re.compile(r"^  - version: ([0-9A-Za-z][0-9A-Za-z.+-]*)$")
    project_re = re.compile(r"^      - repo: ([a-z0-9_.-]+/[a-z0-9_.-]+)$")
    hash_re = re.compile(r"^        hash: ([0-9a-f]{40})$")
    records: list[tuple[str, list[tuple[str, str]]]] = []
    current_version: str | None = None
    current_projects: list[tuple[str, str]] = []
    projects_seen = False
    index = 0
    while index < len(section):
        line = section[index]
        version_match = version_re.fullmatch(line)
        if version_match:
            if current_version is not None:
                if not projects_seen or not current_projects:
                    raise FrozenSourceError(
                        f"release {current_version!r} has no projects in {path}"
                    )
                records.append((current_version, current_projects))
            current_version = version_match.group(1)
            current_projects = []
            projects_seen = False
            index += 1
            continue
        if line == "    projects:":
            if current_version is None or projects_seen:
                raise FrozenSourceError(f"malformed projects section in {path}")
            projects_seen = True
            index += 1
            continue
        project_match = project_re.fullmatch(line)
        if project_match:
            if current_version is None or not projects_seen:
                raise FrozenSourceError(f"project outside a release record in {path}")
            if index + 1 >= len(section):
                raise FrozenSourceError(f"project is missing its hash in {path}")
            hash_match = hash_re.fullmatch(section[index + 1])
            if not hash_match:
                raise FrozenSourceError(f"project has a malformed hash in {path}")
            current_projects.append((project_match.group(1), hash_match.group(1)))
            index += 2
            continue
        if not line or line.lstrip().startswith("#"):
            index += 1
            continue
        raise FrozenSourceError(
            f"unsupported line in releases section at {path}:{start + index + 1}: "
            f"{line!r}"
        )

    if current_version is not None:
        if not projects_seen or not current_projects:
            raise FrozenSourceError(
                f"release {current_version!r} has no projects in {path}"
            )
        records.append((current_version, current_projects))
    matches = [projects for version, projects in records if version == expected_version]
    if len(matches) != 1:
        raise FrozenSourceError(
            "release metadata must contain version "
            f"{expected_version!r} exactly once: {path}"
        )
    projects = matches[0]
    if len(projects) != 1 or projects[0][0] != expected_project:
        raise FrozenSourceError(
            f"release {expected_version!r} must contain only project "
            f"{expected_project!r}: {path}"
        )
    return projects[0][1]


def _git_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return environment


def _run_git(path: Path, arguments: Sequence[str]) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *arguments],
        env=_git_environment(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown git error"
        raise FrozenSourceError(
            f"git {' '.join(arguments)} failed for {path}: {detail}"
        )
    return result.stdout.strip()


def checkout_exact_repository(path: Path, *, repository: str, commit: str) -> None:
    if not SHA_RE.fullmatch(commit):
        raise FrozenSourceError("checkout commit must be a lowercase 40-character SHA")
    if path.exists():
        raise FrozenSourceError(f"refusing to reuse existing checkout path: {path}")
    path.mkdir(parents=False)
    _run_git(path, ["init", "--quiet"])
    _run_git(path, ["remote", "add", "origin", repository])
    _run_git(path, ["fetch", "--quiet", "--no-tags", "--depth=1", "origin", commit])
    _run_git(path, ["checkout", "--quiet", "--detach", commit])
    verify_exact_checkout(path, repository=repository, commit=commit)


def verify_exact_checkout(path: Path, *, repository: str, commit: str) -> None:
    if not path.is_dir():
        raise FrozenSourceError(f"checkout directory does not exist: {path}")
    if _run_git(path, ["remote", "get-url", "origin"]) != repository:
        raise FrozenSourceError(
            f"checkout origin does not match its frozen pin: {path}"
        )
    if _run_git(path, ["cat-file", "-t", commit]) != "commit":
        raise FrozenSourceError(f"frozen object is not a commit: {commit}")
    head = _run_git(path, ["rev-parse", "--verify", "HEAD"])
    if head != commit:
        raise FrozenSourceError(
            f"checkout HEAD {head!r} does not match frozen commit {commit!r}: {path}"
        )
    if _run_git(path, ["diff", "--name-only", "HEAD", "--"]):
        raise FrozenSourceError(f"checkout has modified tracked files: {path}")


def checkout_paths(checkout_root: Path) -> dict[str, Path]:
    return {
        "release_metadata": checkout_root / "releases",
        "kolla": checkout_root / "kolla",
        "kolla_ansible": checkout_root / "kolla-ansible",
    }


def verify_release_metadata(
    checkout: Path, source_contract: dict[str, Any]
) -> None:
    series = source_contract["series"]
    if not isinstance(series, str) or not SERIES_RE.fullmatch(series):
        raise FrozenSourceError("release series is unsafe for a deliverables path")
    deliverables = checkout / "deliverables" / series
    for project, expected_project in DELIVERABLE_PROJECTS.items():
        metadata_path = deliverables / DELIVERABLE_FILES[project]
        actual_commit = parse_deliverable_pin(
            metadata_path,
            expected_project=expected_project,
            expected_version=source_contract[project]["version"],
        )
        expected_commit = source_contract[project]["commit"]
        if actual_commit != expected_commit:
            raise FrozenSourceError(
                f"OpenStack Releases {project} hash {actual_commit!r} does not "
                f"match frozen commit {expected_commit!r}"
            )


def prepare_sources(checkout_root: Path, source_contract: dict[str, Any]) -> None:
    if checkout_root.exists():
        raise FrozenSourceError(
            f"refusing to reuse existing frozen source root: {checkout_root}"
        )
    checkout_root.mkdir(parents=True)
    paths = checkout_paths(checkout_root)
    checkout_exact_repository(
        paths["release_metadata"], **source_contract["release_metadata"]
    )
    verify_release_metadata(paths["release_metadata"], source_contract)
    for project in DELIVERABLE_PROJECTS:
        pin = source_contract[project]
        checkout_exact_repository(
            paths[project], repository=pin["repository"], commit=pin["commit"]
        )


def verify_prepared_sources(
    checkout_root: Path, source_contract: dict[str, Any]
) -> dict[str, Path]:
    paths = checkout_paths(checkout_root)
    verify_exact_checkout(
        paths["release_metadata"], **source_contract["release_metadata"]
    )
    verify_release_metadata(paths["release_metadata"], source_contract)
    for project in DELIVERABLE_PROJECTS:
        pin = source_contract[project]
        verify_exact_checkout(
            paths[project], repository=pin["repository"], commit=pin["commit"]
        )
    return paths


def verify_installed_kolla(source_path: Path, expected_version: str) -> None:
    try:
        distribution = importlib.metadata.distribution("kolla")
    except importlib.metadata.PackageNotFoundError as error:
        raise FrozenSourceError("the Kolla distribution is not installed") from error
    if distribution.version != expected_version:
        raise FrozenSourceError(
            f"installed Kolla version must be {expected_version!r}, got "
            f"{distribution.version!r}"
        )
    raw_direct_url = distribution.read_text("direct_url.json")
    if raw_direct_url is None:
        raise FrozenSourceError(
            "installed Kolla has no direct_url.json source provenance"
        )
    try:
        direct_url = json.loads(
            raw_direct_url, object_pairs_hook=_reject_duplicate_json_keys
        )
    except json.JSONDecodeError as error:
        raise FrozenSourceError("installed Kolla direct_url.json is invalid") from error
    if not isinstance(direct_url, dict) or not isinstance(
        direct_url.get("dir_info"), dict
    ):
        raise FrozenSourceError(
            "installed Kolla was not built from the frozen local source directory"
        )
    parsed = urlparse(direct_url.get("url", ""))
    if parsed.scheme != "file" or parsed.netloc not in ("", "localhost"):
        raise FrozenSourceError("installed Kolla source provenance is not a local path")
    installed_source = Path(unquote(parsed.path)).resolve()
    if installed_source != source_path.resolve():
        raise FrozenSourceError(
            f"installed Kolla source {installed_source} does not match frozen "
            f"checkout {source_path.resolve()}"
        )
    entry_points = [
        entry_point
        for entry_point in distribution.entry_points
        if entry_point.group == "console_scripts" and entry_point.name == "kolla-build"
    ]
    if len(entry_points) != 1 or entry_points[0].value != "kolla.cmd.build:main":
        raise FrozenSourceError(
            "installed Kolla must provide exactly one canonical kolla-build entry point"
        )
    command = Path(sys.executable).parent / "kolla-build"
    if not command.is_file():
        raise FrozenSourceError(
            f"kolla-build is missing beside the active Python: {command}"
        )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prepare and verify exact OpenStack Kolla source checkouts"
    )
    parser.add_argument("command", choices=("prepare", "verify-install"))
    parser.add_argument("--matrix", type=Path, required=True)
    parser.add_argument("--publish-plan", type=Path, required=True)
    parser.add_argument("--checkout-root", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        matrix = load_json_object(args.matrix)
        plan = load_json_object(args.publish_plan)
        source_contract = validate_plan_source_pins(matrix, plan)
        if args.command == "prepare":
            prepare_sources(args.checkout_root, source_contract)
            verify_prepared_sources(args.checkout_root, source_contract)
            print(
                "Prepared exact frozen sources: "
                f"Kolla {source_contract['kolla']['version']}@"
                f"{source_contract['kolla']['commit']}"
            )
        else:
            paths = verify_prepared_sources(args.checkout_root, source_contract)
            verify_installed_kolla(
                paths["kolla"], source_contract["kolla"]["version"]
            )
            print(
                "Verified installed Kolla source provenance: "
                f"{source_contract['kolla']['commit']}"
            )
    except FrozenSourceError as error:
        print(f"Frozen source verification failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
