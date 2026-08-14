from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
from configparser import ConfigParser, Error as ConfigParserError
from io import BytesIO
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import unquote, urlparse

try:
    from scripts.openstack_source_set import (
        FrozenKollaSources,
        OpenStackSourceSetError,
        freeze_kolla_sources,
        validate_frozen_source_contract,
        validate_source_set_release_metadata,
    )
    from scripts.profile_resolver import find_stream
except ModuleNotFoundError:
    from openstack_source_set import (
        FrozenKollaSources,
        OpenStackSourceSetError,
        freeze_kolla_sources,
        validate_frozen_source_contract,
        validate_source_set_release_metadata,
    )
    from profile_resolver import find_stream


RELEASES_REPOSITORY = "https://opendev.org/openstack/releases"
PIN_KEYS = {"repository", "version", "commit"}
METADATA_PIN_KEYS = {"repository", "commit"}
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SERIES_RE = re.compile(r"^[a-z][a-z0-9-]*$")
VERSION_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)+(?:[A-Za-z0-9.-]*)?$")
FROZEN_TAG_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)+(?:[A-Za-z0-9.-]*)?$")
IMAGE_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
DELIVERABLE_PROJECTS = {
    "kolla": "openstack/kolla",
    "kolla_ansible": "openstack/kolla-ansible",
}
DELIVERABLE_FILES = {
    "kolla": "kolla.yaml",
    "kolla_ansible": "kolla-ansible.yaml",
}
KOLLA_BUILD_CONFIG_NAME = "kolla-build.conf"
KOLLA_TEMPLATE_OVERRIDE_NAME = "template-overrides.j2"
BUILD_ENGINE_REQUIRED_VERSIONS = {
    "docker": "7.1.0",
    "pip": "25.3",
    "setuptools": "81.0.0",
}
BUILD_ENGINE_REQUIRED_PACKAGES = frozenset(
    {
        *BUILD_ENGINE_REQUIRED_VERSIONS,
        "gitpython",
        "jinja2",
        "oslo-config",
        "pbr",
    }
)
LOCK_REQUIREMENT_RE = re.compile(
    r"^([A-Za-z0-9][A-Za-z0-9._-]*)=="
    r"([0-9][0-9A-Za-z.!+_-]*)"
    r"((?:\s+--hash=sha256:[0-9a-f]{64})+)$"
)


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


def _canonical_distribution_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _lock_statements(text: str, *, path: Path) -> list[str]:
    statements: list[str] = []
    current = ""
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        if "\t" in raw_line or "\r" in raw_line:
            raise FrozenSourceError(
                f"build-engine lock contains unsupported whitespace at {path}:"
                f"{line_number}"
            )
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            if current:
                raise FrozenSourceError(
                    f"build-engine lock has an interrupted requirement at "
                    f"{path}:{line_number}"
                )
            continue
        continued = stripped.endswith("\\")
        segment = stripped[:-1].rstrip() if continued else stripped
        if not segment:
            raise FrozenSourceError(
                f"build-engine lock has an empty continuation at {path}:{line_number}"
            )
        current = f"{current} {segment}".strip()
        if not continued:
            statements.append(current)
            current = ""
    if current:
        raise FrozenSourceError(
            f"build-engine lock ends with an incomplete continuation: {path}"
        )
    return statements


def load_build_engine_lock(path: Path) -> dict[str, Any]:
    """Load a closed, exact, hash-locked Python build-engine environment."""
    try:
        file_stat = path.lstat()
        content = path.read_bytes()
    except OSError as error:
        raise FrozenSourceError(
            f"cannot read build-engine lock {path}: {error}"
        ) from error
    if not stat.S_ISREG(file_stat.st_mode) or path.is_symlink():
        raise FrozenSourceError(f"build-engine lock must be a regular file: {path}")
    if not content or not content.endswith(b"\n"):
        raise FrozenSourceError(
            f"build-engine lock must be non-empty and end with a newline: {path}"
        )
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise FrozenSourceError(
            f"build-engine lock must be UTF-8: {path}"
        ) from error

    requirements: dict[str, dict[str, Any]] = {}
    for statement in _lock_statements(text, path=path):
        match = LOCK_REQUIREMENT_RE.fullmatch(statement)
        if match is None:
            raise FrozenSourceError(
                "build-engine lock entries must be exact name==version pins with "
                f"only SHA-256 hashes: {statement!r}"
            )
        raw_name, version, raw_hashes = match.groups()
        name = _canonical_distribution_name(raw_name)
        if name in requirements:
            raise FrozenSourceError(
                f"build-engine lock contains duplicate distribution {name!r}"
            )
        hashes = tuple(token.removeprefix("--hash=") for token in raw_hashes.split())
        if not hashes or len(set(hashes)) != len(hashes):
            raise FrozenSourceError(
                f"build-engine lock hashes must be non-empty and unique: {name}"
            )
        requirements[name] = {"version": version, "hashes": hashes}

    missing = sorted(BUILD_ENGINE_REQUIRED_PACKAGES - requirements.keys())
    if missing:
        raise FrozenSourceError(
            f"build-engine lock is missing required distributions: {missing!r}"
        )
    for name, expected_version in BUILD_ENGINE_REQUIRED_VERSIONS.items():
        if requirements[name]["version"] != expected_version:
            raise FrozenSourceError(
                f"build-engine lock must pin {name}=={expected_version}"
            )
    if "kolla" in requirements:
        raise FrozenSourceError(
            "Kolla must be installed from the verified local checkout, not the lock"
        )
    return {
        "sha256": _file_sha256(content),
        "requirements": requirements,
    }


def verify_build_engine_install(
    lock: dict[str, Any], *, kolla_version: str
) -> str:
    """Prove that the active environment is exactly the lock plus local Kolla."""
    requirements = lock.get("requirements")
    lock_digest = lock.get("sha256")
    if not isinstance(requirements, dict) or not isinstance(lock_digest, str):
        raise FrozenSourceError("build-engine lock provenance is invalid")
    expected = {
        name: requirement.get("version")
        for name, requirement in requirements.items()
        if isinstance(name, str) and isinstance(requirement, dict)
    }
    if len(expected) != len(requirements) or any(
        not isinstance(version, str) for version in expected.values()
    ):
        raise FrozenSourceError("build-engine lock requirements are invalid")
    expected["kolla"] = kolla_version

    installed: dict[str, str] = {}
    for distribution in importlib.metadata.distributions():
        raw_name = distribution.metadata.get("Name")
        version = distribution.version
        if not isinstance(raw_name, str) or not isinstance(version, str):
            raise FrozenSourceError(
                "installed distribution has incomplete name/version metadata"
            )
        name = _canonical_distribution_name(raw_name)
        if name in installed:
            raise FrozenSourceError(
                f"active Python contains duplicate distribution metadata: {name}"
            )
        installed[name] = version
    if installed != expected:
        missing = sorted(expected.keys() - installed.keys())
        extra = sorted(installed.keys() - expected.keys())
        changed = sorted(
            name
            for name in expected.keys() & installed.keys()
            if expected[name] != installed[name]
        )
        raise FrozenSourceError(
            "installed build-engine distribution set does not match the lock "
            f"(missing={missing!r}, extra={extra!r}, changed={changed!r})"
        )
    return lock_digest


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

    try:
        openstack_sources = validate_frozen_source_contract(
            plan.get("openstack_sources")
        )
    except OpenStackSourceSetError as error:
        raise FrozenSourceError(
            f"publish plan OpenStack sources are invalid: {error}"
        ) from error
    if openstack_sources["source_set"] != stream.get("source_set"):
        raise FrozenSourceError(
            "publish plan OpenStack source-set does not match the branch matrix stream"
        )
    if openstack_sources["canonical_digest"] != stream.get("source_set_sha256"):
        raise FrozenSourceError(
            "publish plan OpenStack source canonical digest does not match the "
            "branch matrix stream"
        )

    build_images = _build_source_closure(plan)

    return {
        "stream": stream_id,
        "release": stream["release"],
        "series": stream["release_series"],
        "release_metadata": release_metadata,
        "openstack_sources": openstack_sources,
        "build_images": build_images,
        **sources,
    }


def _build_source_closure(plan: dict[str, Any]) -> set[str]:
    build = plan.get("build")
    if not isinstance(build, dict):
        raise FrozenSourceError("publish plan build closure must be an object")
    units = build.get("all_units")
    if not isinstance(units, list) or not units:
        raise FrozenSourceError(
            "publish plan build closure must contain at least one unit"
        )

    images: set[str] = set()
    for index, unit in enumerate(units):
        context = f"publish plan build unit {index}"
        if not isinstance(unit, dict):
            raise FrozenSourceError(f"{context} must be an object")
        target = unit.get("target")
        ancestor_chain = unit.get("ancestor_chain")
        ancestors = unit.get("ancestors")
        if not isinstance(target, str) or not IMAGE_RE.fullmatch(target):
            raise FrozenSourceError(f"{context} target is invalid")
        if (
            not isinstance(ancestor_chain, list)
            or len(ancestor_chain) != len(set(map(str, ancestor_chain)))
            or any(
                not isinstance(image, str) or not IMAGE_RE.fullmatch(image)
                for image in ancestor_chain
            )
        ):
            raise FrozenSourceError(f"{context} ancestor chain is invalid")
        if not isinstance(ancestors, list) or any(
            not isinstance(ancestor, dict)
            or not isinstance(ancestor.get("image"), str)
            or not IMAGE_RE.fullmatch(ancestor["image"])
            for ancestor in ancestors
        ):
            raise FrozenSourceError(f"{context} ancestors are invalid")
        if [ancestor["image"] for ancestor in ancestors] != ancestor_chain:
            raise FrozenSourceError(
                f"{context} ancestors do not match the frozen ancestor chain"
            )
        images.add(target)
        images.update(ancestor_chain)
    return images


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
    if _run_git(
        path,
        ["status", "--porcelain=v1", "--untracked-files=all"],
    ):
        raise FrozenSourceError(
            f"checkout has local changes outside the frozen commit: {path}"
        )


def _git_succeeds(path: Path, arguments: Sequence[str]) -> bool:
    result = subprocess.run(
        ["git", "-C", str(path), *arguments],
        env=_git_environment(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    return result.returncode == 0


def _frozen_mirror_refs(project: dict[str, Any]) -> dict[str, str]:
    build_commit = project.get("build_commit")
    if not isinstance(build_commit, str) or not SHA_RE.fullmatch(build_commit):
        raise FrozenSourceError(
            "project mirror build_commit must be a lowercase 40-character SHA"
        )
    refs = {"refs/heads/frozen": build_commit}
    nearest_release = project.get("nearest_release")
    if nearest_release is None:
        return refs
    if (
        not isinstance(nearest_release, dict)
        or set(nearest_release) != {"version", "commit"}
        or not isinstance(nearest_release.get("version"), str)
        or not FROZEN_TAG_RE.fullmatch(nearest_release["version"])
        or not isinstance(nearest_release.get("commit"), str)
        or not SHA_RE.fullmatch(nearest_release["commit"])
    ):
        raise FrozenSourceError(
            "project mirror nearest_release must be null or an exact safe tag/commit"
        )
    refs[f"refs/tags/{nearest_release['version']}"] = nearest_release["commit"]
    return refs


def prepare_project_mirror(path: Path, project: dict[str, Any]) -> None:
    """Create a closed local Git input with only source-set-owned refs."""
    repository = project.get("repository")
    if not isinstance(repository, str) or not repository:
        raise FrozenSourceError("project mirror repository must be non-empty")
    if path.exists() or path.is_symlink():
        raise FrozenSourceError(f"refusing to reuse project mirror path: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.mkdir()
    _run_git(path, ["init", "--bare", "--quiet"])
    refs = _frozen_mirror_refs(project)
    build_commit = refs["refs/heads/frozen"]

    # Fetching the object ID directly with --no-tags preserves the commit
    # ancestry PBR needs while excluding every mutable upstream ref and tag.
    _run_git(path, ["fetch", "--quiet", "--no-tags", repository, build_commit])
    _run_git(path, ["update-ref", "refs/heads/frozen", build_commit])
    _run_git(path, ["symbolic-ref", "HEAD", "refs/heads/frozen"])
    for ref, commit in refs.items():
        if ref == "refs/heads/frozen":
            continue
        if _run_git(path, ["cat-file", "-t", commit]) != "commit":
            raise FrozenSourceError(
                f"frozen release object is not a commit: {commit}"
            )
        if not _git_succeeds(
            path, ["merge-base", "--is-ancestor", commit, build_commit]
        ):
            raise FrozenSourceError(
                "frozen nearest release commit is not an ancestor of build_commit"
            )
        _run_git(path, ["update-ref", ref, commit])
    try:
        (path / "FETCH_HEAD").unlink(missing_ok=True)
    except OSError as error:
        raise FrozenSourceError(
            f"cannot remove transient project mirror FETCH_HEAD: {path}: {error}"
        ) from error
    verify_project_mirror(path, project)


def verify_project_mirror(path: Path, project: dict[str, Any]) -> None:
    """Fail closed unless a local mirror contains exactly the frozen graph."""
    if path.is_symlink() or not path.is_dir():
        raise FrozenSourceError(f"project mirror is missing or unsafe: {path}")
    expected_refs = _frozen_mirror_refs(project)
    build_commit = expected_refs["refs/heads/frozen"]
    if _run_git(path, ["rev-parse", "--is-bare-repository"]) != "true":
        raise FrozenSourceError(f"project mirror must be bare: {path}")
    if _run_git(path, ["remote"]):
        raise FrozenSourceError(f"project mirror must not retain a remote: {path}")
    if _run_git(path, ["symbolic-ref", "HEAD"]) != "refs/heads/frozen":
        raise FrozenSourceError(f"project mirror HEAD is not frozen: {path}")
    if _run_git(path, ["rev-parse", "HEAD"]) != build_commit:
        raise FrozenSourceError(
            f"project mirror HEAD does not match build_commit: {path}"
        )
    if _run_git(path, ["cat-file", "-t", build_commit]) != "commit":
        raise FrozenSourceError(
            f"project mirror build object is not a commit: {build_commit}"
        )
    actual_refs: dict[str, str] = {}
    raw_refs = _run_git(path, ["for-each-ref", "--format=%(refname) %(objectname)"])
    for record in raw_refs.splitlines() if raw_refs else ():
        ref, separator, commit = record.partition(" ")
        if not separator or ref in actual_refs:
            raise FrozenSourceError(f"project mirror has malformed refs: {path}")
        actual_refs[ref] = commit
    if actual_refs != expected_refs:
        raise FrozenSourceError(
            "project mirror refs do not exactly match the source-set "
            f"(expected={sorted(expected_refs)!r}, actual={sorted(actual_refs)!r})"
        )
    for ref, commit in expected_refs.items():
        if _run_git(path, ["rev-parse", f"{ref}^{{commit}}"]) != commit:
            raise FrozenSourceError(f"project mirror ref target is invalid: {ref}")
    nearest_release = project.get("nearest_release")
    if nearest_release is not None and not _git_succeeds(
        path,
        [
            "merge-base",
            "--is-ancestor",
            nearest_release["commit"],
            build_commit,
        ],
    ):
        raise FrozenSourceError(
            "frozen nearest release commit is not an ancestor of build_commit"
        )
    forbidden_paths = (
        path / "FETCH_HEAD",
        path / "shallow",
        path / "objects" / "info" / "alternates",
        path / "info" / "grafts",
    )
    if any(candidate.exists() or candidate.is_symlink() for candidate in forbidden_paths):
        raise FrozenSourceError(
            f"project mirror contains transient or external graph state: {path}"
        )
    unreachable = _run_git(
        path, ["fsck", "--strict", "--no-reflogs", "--unreachable"]
    )
    if unreachable:
        raise FrozenSourceError(
            f"project mirror contains objects outside the frozen refs: {path}"
        )


def _pbr_project_metadata(worktree: Path) -> tuple[str, str | None] | None:
    setup_cfg = worktree / "setup.cfg"
    if not setup_cfg.is_file() or setup_cfg.is_symlink():
        return None
    parser = ConfigParser(interpolation=None)
    try:
        with setup_cfg.open(encoding="utf-8") as file_obj:
            parser.read_file(file_obj)
        name = parser.get("metadata", "name")
        pre_version = parser.get("metadata", "version", fallback=None)
    except (OSError, UnicodeError, ConfigParserError, KeyError) as error:
        raise FrozenSourceError(
            f"cannot read project package name from {setup_cfg}: {error}"
        ) from error
    name = name.strip()
    if not name or "\n" in name or "\r" in name:
        raise FrozenSourceError(f"project package name is invalid: {setup_cfg}")
    if pre_version is not None:
        pre_version = pre_version.strip()
        if not pre_version or "\n" in pre_version or "\r" in pre_version:
            raise FrozenSourceError(
                f"project pre-version is invalid: {setup_cfg}"
            )
    return name, pre_version


def _pbr_project_version(
    worktree: Path,
    package_name: str,
    pre_version: str | None,
    *,
    python_executable: Path,
) -> str:
    script = (
        "import pbr.packaging; "
        "print(pbr.packaging.get_version("
        + repr(package_name)
        + ", "
        + repr(pre_version)
        + "))"
    )
    environment = _git_environment()
    environment.pop("PBR_VERSION", None)
    environment.pop("OSLO_PACKAGE_VERSION", None)
    result = subprocess.run(
        [str(python_executable), "-c", script],
        cwd=worktree,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown PBR error"
        raise FrozenSourceError(
            f"cannot derive the frozen PBR version for {package_name}: {detail}"
        )
    version = result.stdout.strip()
    if not version or "\n" in version or "\r" in version:
        raise FrozenSourceError(
            f"PBR returned an invalid frozen version for {package_name!r}"
        )
    return version


def _archive_member(
    archive: tarfile.TarFile,
    *,
    name: str,
    mode: int,
    data: bytes | None = None,
    linkname: str = "",
) -> None:
    info = tarfile.TarInfo(name=name)
    info.uid = info.gid = 0
    info.uname = info.gname = "root"
    info.mtime = 0
    info.mode = mode
    if data is not None:
        info.type = tarfile.REGTYPE
        info.size = len(data)
        archive.addfile(info, BytesIO(data))
    elif linkname:
        info.type = tarfile.SYMTYPE
        info.linkname = linkname
        archive.addfile(info)
    else:
        info.type = tarfile.DIRTYPE
        archive.addfile(info)


def prepare_project_archive(
    mirror: Path,
    archive_path: Path,
    project: dict[str, Any],
    *,
    archive_root: str,
    python_executable: Path | None = None,
) -> str | None:
    """Export one exact commit into a deterministic Kolla local-source tar."""
    verify_project_mirror(mirror, project)
    if archive_path.exists() or archive_path.is_symlink():
        raise FrozenSourceError(
            f"refusing to replace project source archive: {archive_path}"
        )
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="frozen-source-export-") as temp_dir:
        worktree = Path(temp_dir) / "source"
        subprocess_result = subprocess.run(
            ["git", "clone", "--quiet", "--no-hardlinks", str(mirror), str(worktree)],
            env=_git_environment(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if subprocess_result.returncode != 0:
            detail = (
                subprocess_result.stderr.strip()
                or subprocess_result.stdout.strip()
                or "unknown git clone error"
            )
            raise FrozenSourceError(
                f"cannot create frozen project export worktree: {detail}"
            )
        build_commit = project["build_commit"]
        if _run_git(worktree, ["rev-parse", "HEAD"]) != build_commit:
            raise FrozenSourceError("project export worktree is not at build_commit")
        raw_paths = subprocess.run(
            ["git", "-C", str(worktree), "ls-files", "-z"],
            env=_git_environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if raw_paths.returncode != 0:
            raise FrozenSourceError(
                "cannot enumerate the frozen project tracked files: "
                + raw_paths.stderr.decode("utf-8", errors="replace").strip()
            )
        tracked = sorted(
            path.decode("utf-8")
            for path in raw_paths.stdout.split(b"\0")
            if path
        )
        if not tracked or any(
            not path
            or path.startswith("/")
            or ".." in Path(path).parts
            or ".git" in Path(path).parts
            for path in tracked
        ):
            raise FrozenSourceError("project export tracked-file list is unsafe")
        package_metadata = _pbr_project_metadata(worktree)
        if package_metadata is not None and python_executable is None:
            raise FrozenSourceError(
                "a verified build-engine Python is required for PBR source export"
            )
        package_version = None
        package_name: str | None = None
        if package_metadata is not None:
            assert python_executable is not None
            package_name, pre_version = package_metadata
            package_version = _pbr_project_version(
                worktree,
                package_name,
                pre_version,
                python_executable=python_executable,
            )
        root_name = archive_root
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*-archive-[0-9a-f]{40}", root_name):
            raise FrozenSourceError(
                f"project source archive root name is unsafe: {root_name!r}"
            )
        # Kolla extracts plugin/addition inputs into a freshly-created
        # aggregate directory and then re-tars that directory.  The explicit
        # `.` member applies normalized metadata to that aggregate extraction
        # root, closing Kolla's otherwise run-time-dependent root mtime.
        directory_names = {".", root_name}
        for relative in tracked:
            parent = Path(relative).parent
            while parent != Path("."):
                directory_names.add(f"{root_name}/{parent.as_posix()}")
                parent = parent.parent

        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=archive_path.parent,
                prefix=f".{archive_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as file_obj:
                temporary_path = Path(file_obj.name)
            source_members = {
                f"{root_name}/{relative}": worktree / relative
                for relative in tracked
            }
            if package_name is not None and package_version is not None:
                pkg_info_name = f"{root_name}/PKG-INFO"
                if pkg_info_name in source_members:
                    raise FrozenSourceError(
                        "project export commit already contains root PKG-INFO"
                    )
                pkg_info = (
                    "Metadata-Version: 2.1\n"
                    f"Name: {package_name}\n"
                    f"Version: {package_version}\n"
                ).encode("utf-8")
            else:
                pkg_info_name = ""
                pkg_info = b""
            all_member_names = sorted(
                directory_names | set(source_members) | ({pkg_info_name} if pkg_info_name else set())
            )
            with tarfile.open(temporary_path, "w", format=tarfile.PAX_FORMAT) as archive:
                for member_name in all_member_names:
                    if member_name in directory_names:
                        _archive_member(archive, name=member_name, mode=0o755)
                        continue
                    if member_name == pkg_info_name:
                        _archive_member(
                            archive,
                            name=member_name,
                            mode=0o644,
                            data=pkg_info,
                        )
                        continue
                    source = source_members[member_name]
                    relative = member_name.removeprefix(f"{root_name}/")
                    file_stat = source.lstat()
                    if stat.S_ISREG(file_stat.st_mode):
                        mode = 0o755 if file_stat.st_mode & 0o111 else 0o644
                        _archive_member(
                            archive,
                            name=member_name,
                            mode=mode,
                            data=source.read_bytes(),
                        )
                    elif stat.S_ISLNK(file_stat.st_mode):
                        linkname = os.readlink(source)
                        if os.path.isabs(linkname):
                            raise FrozenSourceError(
                                f"project export contains an absolute symlink: {relative}"
                            )
                        _archive_member(
                            archive,
                            name=member_name,
                            mode=0o777,
                            linkname=linkname,
                        )
                    else:
                        raise FrozenSourceError(
                            f"project export contains an unsupported file type: {relative}"
                        )
            os.link(temporary_path, archive_path)
        except (OSError, tarfile.TarError) as error:
            raise FrozenSourceError(
                f"cannot create deterministic project source archive: {error}"
            ) from error
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        return package_version


def _unit_from_plan(plan: dict[str, Any], unit_id: str) -> dict[str, Any]:
    try:
        units = plan["build"]["all_units"]
    except (KeyError, TypeError) as error:
        raise FrozenSourceError("publish plan has no build unit list") from error
    if not isinstance(unit_id, str) or not unit_id:
        raise FrozenSourceError("source archive unit ID must be non-empty")
    matches = [
        unit
        for unit in units
        if isinstance(unit, dict) and unit.get("id") == unit_id
    ]
    if len(matches) != 1:
        raise FrozenSourceError(
            f"publish plan must contain exactly one source archive unit: {unit_id}"
        )
    unit = matches[0]
    target = unit.get("target")
    ancestors = unit.get("ancestor_chain")
    if (
        not isinstance(target, str)
        or not IMAGE_RE.fullmatch(target)
        or not isinstance(ancestors, list)
        or any(
            not isinstance(image, str) or not IMAGE_RE.fullmatch(image)
            for image in ancestors
        )
    ):
        raise FrozenSourceError(f"publish plan source archive unit is invalid: {unit_id}")
    return unit


def _section_archive_path(source_archive_dir: Path, section: str) -> Path:
    if not IMAGE_RE.fullmatch(section):
        raise FrozenSourceError(
            f"source archive Kolla section is invalid: {section!r}"
        )
    return source_archive_dir / f"{section}.tar"


def _unit_source_sections(
    source_contract: dict[str, Any], plan: dict[str, Any], unit_id: str
) -> dict[str, tuple[str, dict[str, Any]]]:
    unit = _unit_from_plan(plan, unit_id)
    target = unit["target"]
    projects = source_contract["openstack_sources"]["source_set"]["projects"]
    selected = {
        section: (project_name, project)
        for project_name, project in projects.items()
        for section in project["kolla_sections"]
        if (
            section == target
            or section.startswith(f"{target}-plugin-")
            or section.startswith(f"{target}-additions-")
        )
    }
    return dict(sorted(selected.items()))


def _section_archive_root(section: str, project: dict[str, Any]) -> str:
    build_commit = project.get("build_commit")
    if not IMAGE_RE.fullmatch(section) or not isinstance(
        build_commit, str
    ) or not SHA_RE.fullmatch(build_commit):
        raise FrozenSourceError("source archive section/build commit is invalid")
    # This exactly preserves Kolla's Git-source clone directory convention for
    # the formerly rendered `reference = <build_commit>` contract.
    return f"{section}-archive-{build_commit}"


def _verify_project_archive_shape(archive_path: Path) -> None:
    if archive_path.is_symlink() or not archive_path.is_file():
        raise FrozenSourceError(
            f"project source archive is missing or unsafe: {archive_path}"
        )
    try:
        with tarfile.open(archive_path, "r:") as archive:
            members = archive.getmembers()
            names = [member.name for member in members]
            if not names or names != sorted(names) or len(names) != len(set(names)):
                raise FrozenSourceError(
                    f"project source archive member order is invalid: {archive_path}"
                )
            roots = {Path(name).parts[0] for name in names if Path(name).parts}
            if len(roots) != 1:
                raise FrozenSourceError(
                    f"project source archive must have one top-level directory: {archive_path}"
                )
            for member in members:
                parts = Path(member.name).parts
                if member.name == ".":
                    if (
                        not member.isdir()
                        or member.uid != 0
                        or member.gid != 0
                        or member.uname != "root"
                        or member.gname != "root"
                        or member.mtime != 0
                        or member.mode != 0o755
                    ):
                        raise FrozenSourceError(
                            "project source archive extraction-root metadata "
                            f"is unsafe: {archive_path}"
                        )
                    continue
                if (
                    not parts
                    or member.name.startswith("/")
                    or ".." in parts
                    or ".git" in parts
                    or member.uid != 0
                    or member.gid != 0
                    or member.uname != "root"
                    or member.gname != "root"
                    or member.mtime != 0
                    or not (member.isdir() or member.isfile() or member.issym())
                    or (member.issym() and os.path.isabs(member.linkname))
                ):
                    raise FrozenSourceError(
                        f"project source archive member is unsafe: {member.name}"
                    )
    except (OSError, tarfile.TarError) as error:
        raise FrozenSourceError(
            f"cannot verify project source archive {archive_path}: {error}"
        ) from error


def verify_project_archive(
    archive_path: Path,
    project: dict[str, Any],
    *,
    archive_root: str,
    mirror: Path,
    python_executable: Path,
) -> None:
    """Bind a normalized archive byte-for-byte to its closed Git mirror."""
    _verify_project_archive_shape(archive_path)
    with tempfile.TemporaryDirectory(prefix="frozen-source-verify-") as temp_dir:
        expected_path = Path(temp_dir) / archive_path.name
        prepare_project_archive(
            mirror,
            expected_path,
            project,
            archive_root=archive_root,
            python_executable=python_executable,
        )
        try:
            actual = archive_path.read_bytes()
            expected = expected_path.read_bytes()
        except OSError as error:
            raise FrozenSourceError(
                f"cannot compare frozen project source archive: {error}"
            ) from error
        if not actual or actual != expected:
            raise FrozenSourceError(
                "project source archive bytes do not match the frozen Git commit"
            )


def prepare_unit_source_archives(
    checkout_root: Path,
    source_contract: dict[str, Any],
    plan: dict[str, Any],
    *,
    unit_id: str,
    source_archive_dir: Path,
    python_executable: Path,
) -> None:
    if source_archive_dir.exists() or source_archive_dir.is_symlink():
        raise FrozenSourceError(
            f"refusing to reuse source archive directory: {source_archive_dir}"
        )
    source_archive_dir.mkdir(parents=True)
    mirror_dir = checkout_root / "project-mirrors"
    if mirror_dir.exists() or mirror_dir.is_symlink():
        raise FrozenSourceError(f"refusing to reuse project mirror directory: {mirror_dir}")
    mirror_dir.mkdir(parents=True)
    selected = _unit_source_sections(source_contract, plan, unit_id)
    mirrors: dict[str, Path] = {}
    selected_projects = {
        project_name: project
        for project_name, project in selected.values()
    }
    for project_name, project in sorted(selected_projects.items()):
        mirror_path = mirror_dir / f"{project_name.replace('/', '__')}.git"
        prepare_project_mirror(mirror_path, project)
        mirrors[project_name] = mirror_path
    for section, (project_name, project) in selected.items():
        mirror_path = mirrors[project_name]
        archive_path = _section_archive_path(source_archive_dir, section)
        archive_root = _section_archive_root(section, project)
        prepare_project_archive(
            mirror_path,
            archive_path,
            project,
            archive_root=archive_root,
            python_executable=python_executable,
        )
        verify_project_archive(
            archive_path,
            project,
            archive_root=archive_root,
            mirror=mirror_path,
            python_executable=python_executable,
        )


def verify_unit_source_archives(
    checkout_root: Path,
    source_contract: dict[str, Any],
    plan: dict[str, Any],
    *,
    unit_id: str,
    source_archive_dir: Path,
    python_executable: Path,
) -> None:
    selected = _unit_source_sections(source_contract, plan, unit_id)
    if source_archive_dir.is_symlink() or not source_archive_dir.is_dir():
        raise FrozenSourceError(
            f"source archive directory is missing or unsafe: {source_archive_dir}"
        )
    expected_paths = {
        _section_archive_path(source_archive_dir, section)
        for section in selected
    }
    actual_paths = set(source_archive_dir.iterdir())
    if actual_paths != expected_paths:
        raise FrozenSourceError(
            "source archive directory does not exactly match the selected unit"
        )
    for section, (project_name, project) in selected.items():
        verify_project_archive(
            _section_archive_path(source_archive_dir, section),
            project,
            archive_root=_section_archive_root(section, project),
            mirror=(
                checkout_root
                / "project-mirrors"
                / f"{project_name.replace('/', '__')}.git"
            ),
            python_executable=python_executable,
        )


def checkout_paths(checkout_root: Path) -> dict[str, Path]:
    return {
        "release_metadata": checkout_root / "releases",
        "kolla": checkout_root / "kolla",
        "kolla_ansible": checkout_root / "kolla-ansible",
        "requirements": checkout_root / "requirements",
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


def _verify_openstack_source_closure(
    paths: dict[str, Path], source_contract: dict[str, Any]
) -> FrozenKollaSources:
    source_provenance = source_contract["openstack_sources"]
    try:
        validate_source_set_release_metadata(
            source_provenance["source_set"], paths["release_metadata"]
        )
        frozen = freeze_kolla_sources(
            source_provenance["source_set"],
            paths["kolla"] / "kolla" / "common" / "sources.py",
            images=source_contract["build_images"],
            toolchain_version=source_contract["kolla"]["version"],
        )
    except OpenStackSourceSetError as error:
        raise FrozenSourceError(
            f"frozen Kolla source closure is invalid: {error}"
        ) from error

    actual_files = {
        "kolla_build_config": {
            "sha256": frozen.config_sha256,
            "content": frozen.config_content,
        },
        "template_override": {
            "sha256": frozen.template_override_sha256,
            "content": frozen.template_override_content,
        },
    }
    for name, actual in actual_files.items():
        if source_provenance[name] != actual:
            raise FrozenSourceError(
                f"frozen Kolla source {name} does not match the publish plan"
            )
    return frozen


def _requirements_pin(source_contract: dict[str, Any]) -> dict[str, Any]:
    try:
        pin = source_contract["openstack_sources"]["source_set"]["projects"][
            "openstack/requirements"
        ]
    except (KeyError, TypeError) as error:
        raise FrozenSourceError(
            "frozen source-set has no OpenStack requirements pin"
        ) from error
    return pin


def _verify_requirements_constraints(
    requirements_checkout: Path, source_contract: dict[str, Any]
) -> None:
    pin = _requirements_pin(source_contract)
    path = requirements_checkout / "upper-constraints.txt"
    try:
        content = path.read_bytes()
    except OSError as error:
        raise FrozenSourceError(
            f"cannot read frozen upper constraints {path}: {error}"
        ) from error
    if not content:
        raise FrozenSourceError("frozen upper constraints must not be empty")
    actual = hashlib.sha256(content).hexdigest()
    if actual != pin["upper_constraints_sha256"]:
        raise FrozenSourceError(
            "frozen upper constraints digest does not match the source-set"
        )


def _file_sha256(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _materialized_files(
    build_config_dir: Path, source_contract: dict[str, Any]
) -> dict[Path, dict[str, str]]:
    source_provenance = source_contract["openstack_sources"]
    return {
        build_config_dir / KOLLA_BUILD_CONFIG_NAME: source_provenance[
            "kolla_build_config"
        ],
        build_config_dir / KOLLA_TEMPLATE_OVERRIDE_NAME: source_provenance[
            "template_override"
        ],
    }


def verify_materialized_configs(
    build_config_dir: Path, source_contract: dict[str, Any]
) -> None:
    if build_config_dir.is_symlink() or not build_config_dir.is_dir():
        raise FrozenSourceError(
            f"frozen build config directory is missing or unsafe: {build_config_dir}"
        )
    for path, expected in _materialized_files(
        build_config_dir, source_contract
    ).items():
        try:
            file_stat = path.lstat()
            content = path.read_bytes()
        except (OSError, UnicodeError) as error:
            raise FrozenSourceError(
                f"cannot read frozen build config {path}: {error}"
            ) from error
        if not stat.S_ISREG(file_stat.st_mode) or path.is_symlink():
            raise FrozenSourceError(f"frozen build config is not a regular file: {path}")
        expected_bytes = expected["content"].encode("utf-8")
        if content != expected_bytes:
            raise FrozenSourceError(
                f"frozen build config content does not match the publish plan: {path}"
            )
        if _file_sha256(content) != expected["sha256"]:
            raise FrozenSourceError(
                f"frozen build config digest does not match the publish plan: {path}"
            )


def materialize_frozen_configs(
    build_config_dir: Path, source_contract: dict[str, Any]
) -> None:
    if build_config_dir.exists():
        if build_config_dir.is_symlink() or not build_config_dir.is_dir():
            raise FrozenSourceError(
                f"refusing unsafe frozen build config directory: {build_config_dir}"
            )
    else:
        try:
            build_config_dir.mkdir(parents=True)
        except OSError as error:
            raise FrozenSourceError(
                f"cannot create frozen build config directory {build_config_dir}: {error}"
            ) from error

    for path, expected in _materialized_files(
        build_config_dir, source_contract
    ).items():
        content = expected["content"]
        if path.exists() or path.is_symlink():
            if path.is_symlink() or not path.is_file():
                raise FrozenSourceError(
                    f"refusing unsafe frozen build config path: {path}"
                )
            try:
                existing = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as error:
                raise FrozenSourceError(
                    f"cannot read existing frozen build config {path}: {error}"
                ) from error
            if existing != content:
                raise FrozenSourceError(
                    f"refusing to replace mismatched frozen build config: {path}"
                )
            continue
        try:
            with path.open("x", encoding="utf-8", newline="") as file_obj:
                file_obj.write(content)
        except (OSError, UnicodeError) as error:
            raise FrozenSourceError(
                f"cannot materialize frozen build config {path}: {error}"
            ) from error
    verify_materialized_configs(build_config_dir, source_contract)


def prepare_sources(
    checkout_root: Path,
    source_contract: dict[str, Any],
    *,
    build_config_dir: Path,
) -> None:
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
    requirements_pin = _requirements_pin(source_contract)
    checkout_exact_repository(
        paths["requirements"],
        repository=requirements_pin["repository"],
        commit=requirements_pin["build_commit"],
    )
    _verify_requirements_constraints(paths["requirements"], source_contract)
    _verify_openstack_source_closure(paths, source_contract)
    materialize_frozen_configs(build_config_dir, source_contract)


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
    requirements_pin = _requirements_pin(source_contract)
    verify_exact_checkout(
        paths["requirements"],
        repository=requirements_pin["repository"],
        commit=requirements_pin["build_commit"],
    )
    _verify_requirements_constraints(paths["requirements"], source_contract)
    _verify_openstack_source_closure(paths, source_contract)
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
    parser.add_argument(
        "command", choices=("prepare", "prepare-unit-sources", "verify-install")
    )
    parser.add_argument("--matrix", type=Path, required=True)
    parser.add_argument("--publish-plan", type=Path, required=True)
    parser.add_argument("--checkout-root", type=Path, required=True)
    parser.add_argument("--build-config-dir", type=Path, required=True)
    parser.add_argument("--build-engine-lock", type=Path, required=True)
    parser.add_argument("--unit-id")
    parser.add_argument("--source-archive-dir", type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        matrix = load_json_object(args.matrix)
        plan = load_json_object(args.publish_plan)
        build_engine_lock = load_build_engine_lock(args.build_engine_lock)
        source_contract = validate_plan_source_pins(matrix, plan)
        if args.command == "prepare":
            if args.unit_id is not None or args.source_archive_dir is not None:
                raise FrozenSourceError(
                    "prepare does not accept unit source archive arguments"
                )
            prepare_sources(
                args.checkout_root,
                source_contract,
                build_config_dir=args.build_config_dir,
            )
            verify_prepared_sources(args.checkout_root, source_contract)
            verify_materialized_configs(args.build_config_dir, source_contract)
            print(
                "Prepared exact frozen sources: "
                f"Kolla {source_contract['kolla']['version']}@"
                f"{source_contract['kolla']['commit']}"
            )
        elif args.command == "prepare-unit-sources":
            if not args.unit_id or args.source_archive_dir is None:
                raise FrozenSourceError(
                    "prepare-unit-sources requires --unit-id and --source-archive-dir"
                )
            paths = verify_prepared_sources(args.checkout_root, source_contract)
            verify_materialized_configs(args.build_config_dir, source_contract)
            verify_installed_kolla(
                paths["kolla"], source_contract["kolla"]["version"]
            )
            lock_digest = verify_build_engine_install(
                build_engine_lock,
                kolla_version=source_contract["kolla"]["version"],
            )
            prepare_unit_source_archives(
                args.checkout_root,
                source_contract,
                plan,
                unit_id=args.unit_id,
                source_archive_dir=args.source_archive_dir,
                python_executable=Path(sys.executable),
            )
            verify_unit_source_archives(
                args.checkout_root,
                source_contract,
                plan,
                unit_id=args.unit_id,
                source_archive_dir=args.source_archive_dir,
                python_executable=Path(sys.executable),
            )
            print(
                f"Prepared frozen source archives for {args.unit_id}; "
                f"build engine {lock_digest}"
            )
        else:
            if not args.unit_id or args.source_archive_dir is None:
                raise FrozenSourceError(
                    "verify-install requires --unit-id and --source-archive-dir"
                )
            paths = verify_prepared_sources(args.checkout_root, source_contract)
            verify_materialized_configs(args.build_config_dir, source_contract)
            verify_installed_kolla(
                paths["kolla"], source_contract["kolla"]["version"]
            )
            lock_digest = verify_build_engine_install(
                build_engine_lock,
                kolla_version=source_contract["kolla"]["version"],
            )
            verify_unit_source_archives(
                args.checkout_root,
                source_contract,
                plan,
                unit_id=args.unit_id,
                source_archive_dir=args.source_archive_dir,
                python_executable=Path(sys.executable),
            )
            print(
                "Verified installed Kolla source provenance: "
                f"{source_contract['kolla']['commit']}; build engine {lock_digest}"
            )
    except FrozenSourceError as error:
        print(f"Frozen source verification failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
