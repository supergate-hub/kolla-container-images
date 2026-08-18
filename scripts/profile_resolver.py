from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

try:
    from scripts.openstack_source_set import (
        load_source_set,
        validate_source_set_toolchain,
    )
    from scripts.release_policy import release_branch_for
except ModuleNotFoundError:
    from openstack_source_set import load_source_set, validate_source_set_toolchain
    from release_policy import release_branch_for


ROOT = Path(__file__).resolve().parents[1]
MATRIX_PATH = ROOT / "config" / "build-matrix.json"
PROFILES_DIR = ROOT / "config" / "profiles"
SOURCE_SETS_DIR = ROOT / "config" / "openstack-sources"
SELECTOR_FIELDS = {"streams": "id", "releases": "release", "distros": "distro"}
LOCAL_DRY_RUN_CANDIDATE_ID = "local-dry-run"
CANDIDATE_ID_RE = re.compile(r"^[1-9][0-9]*-[1-9][0-9]*$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
RELEASE_KEYS = {"series", "source_set"}
TOOLCHAIN_KEYS = {"kolla", "kolla_ansible"}
BASE_KEYS = {"distro", "os_version", "image", "tag"}
SOURCE_PIN_KEYS = {"repository", "commit"}
SOURCE_REPOSITORIES = {
    "kolla": "https://opendev.org/openstack/kolla",
    "kolla_ansible": "https://opendev.org/openstack/kolla-ansible",
}
RESOLVED_TOOLCHAIN_FIELDS = {
    "release_series",
    "release_branch",
    "kolla_repository",
    "kolla_version",
    "kolla_commit",
    "kolla_ansible_repository",
    "kolla_ansible_version",
    "kolla_ansible_commit",
    "toolchain_version",
    "base_id",
    "base_image",
    "base_tag",
    "distro",
    "os_version",
    "tag_token",
    "source_set_id",
    "source_set",
    "source_set_sha256",
}


class Matrix(dict[str, Any]):
    """Raw matrix mapping carrying the directory used for relative references."""

    def __init__(self, value: dict[str, Any], *, source_sets_dir: Path) -> None:
        super().__init__(value)
        self.source_sets_dir = source_sets_dir


def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key!r}")
        value[key] = item
    return value


def load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as file_obj:
        return json.load(file_obj, object_pairs_hook=reject_duplicate_keys)


def load_matrix(path: Path = MATRIX_PATH) -> dict[str, Any]:
    matrix = load_json(path)
    if not isinstance(matrix, dict):
        raise ValueError(f"matrix must be an object: {path}")
    return Matrix(matrix, source_sets_dir=path.parent / "openstack-sources")


def stream_ids(matrix: dict[str, Any]) -> list[str]:
    return [stream["id"] for stream in matrix["streams"]]


def _tag_aliases(matrix: dict[str, Any]) -> dict[str, str]:
    aliases = matrix.get("tag_aliases", {})
    if not isinstance(aliases, dict):
        raise ValueError("matrix tag_aliases must be an object")
    if any(
        not isinstance(alias, str) or not alias
        or not isinstance(target, str) or not target
        for alias, target in aliases.items()
    ):
        raise ValueError("matrix tag_aliases must map non-empty strings to non-empty strings")
    return aliases


def tag_aliases_for_stream(
    matrix: dict[str, Any],
    stream: dict[str, Any],
) -> list[str]:
    """Return configured mutable aliases that point at one exact stream."""
    stream_id = stream.get("id")
    if not isinstance(stream_id, str) or not stream_id:
        raise ValueError("stream id must be a non-empty string")
    return sorted(
        alias
        for alias, target in _tag_aliases(matrix).items()
        if target == stream_id
    )


def resolve_tag_alias(matrix: dict[str, Any], alias: str) -> dict[str, Any]:
    """Resolve one user-facing alias to its exact configured stream."""
    if not isinstance(alias, str) or not alias:
        raise ValueError("tag alias must be a non-empty string")
    target = _tag_aliases(matrix).get(alias)
    if target is None:
        accepted = ", ".join(sorted(_tag_aliases(matrix))) or "none"
        raise ValueError(f"unknown tag alias {alias!r}; accepted aliases: {accepted}")
    return find_stream(matrix, target)


def find_toolchain(matrix: dict[str, Any], version: str) -> dict[str, Any]:
    toolchains = matrix.get("toolchains")
    if not isinstance(toolchains, dict):
        raise ValueError("matrix toolchains must be an object")
    toolchain = toolchains.get(version)
    if not isinstance(toolchain, dict):
        accepted = ", ".join(sorted(map(str, toolchains)))
        raise ValueError(
            f"unsupported toolchain version: {version}; accepted versions: {accepted}"
        )
    return toolchain


def resolve_stream(
    matrix: dict[str, Any],
    stream: dict[str, Any],
) -> dict[str, Any]:
    """Join a v4 stream's release, toolchain, and base references."""
    conflicting_fields = set(stream) & RESOLVED_TOOLCHAIN_FIELDS
    if conflicting_fields:
        raise ValueError(
            "stream must inherit resolved fields by reference; conflicting fields: "
            f"{sorted(conflicting_fields)!r}"
        )
    release = stream.get("release")
    if not isinstance(release, str):
        raise ValueError("stream release must be a string")
    releases = matrix.get("releases")
    if not isinstance(releases, dict):
        raise ValueError("matrix releases must be an object")
    release_config = releases.get(release)
    if not isinstance(release_config, dict):
        accepted = ", ".join(sorted(map(str, releases)))
        raise ValueError(
            f"unsupported release: {release}; accepted releases: {accepted}"
        )
    if set(release_config) != RELEASE_KEYS:
        raise ValueError(
            f"release {release!r} keys must be exactly {sorted(RELEASE_KEYS)!r}"
        )

    toolchain_version = stream.get("toolchain")
    if not isinstance(toolchain_version, str):
        raise ValueError("stream toolchain must be a string")
    toolchain = find_toolchain(matrix, toolchain_version)
    if set(toolchain) != TOOLCHAIN_KEYS:
        raise ValueError(
            f"toolchain {toolchain_version!r} keys must be exactly "
            f"{sorted(TOOLCHAIN_KEYS)!r}"
        )
    expected_branch = release_branch_for(release)
    series = release_config.get("series")
    if not isinstance(series, str) or not series:
        raise ValueError(f"release {release!r} series must be a string")
    source_set_id = release_config.get("source_set")
    if not isinstance(source_set_id, str) or not source_set_id:
        raise ValueError(
            f"release {release!r} source_set must be a string"
        )
    source_sets_dir = getattr(matrix, "source_sets_dir", SOURCE_SETS_DIR)
    source_set = load_source_set(
        source_sets_dir / f"{source_set_id}.json",
        expected_id=source_set_id,
        expected_release=release,
        expected_series=series,
    )
    if source_set.document["schema_version"] != 3:
        raise ValueError(
            f"active source-set schema_version must be 3: {source_set_id!r}"
        )

    sources: dict[str, dict[str, str]] = {}
    for project, expected_repository in SOURCE_REPOSITORIES.items():
        source = toolchain.get(project)
        if not isinstance(source, dict) or set(source) != SOURCE_PIN_KEYS:
            raise ValueError(
                f"toolchain {toolchain_version!r} {project} pin keys must be exactly "
                f"{sorted(SOURCE_PIN_KEYS)!r}"
            )
        repository = source.get("repository")
        commit = source.get("commit")
        if repository != expected_repository:
            raise ValueError(
                f"toolchain {toolchain_version!r} {project} repository must be "
                f"{expected_repository!r}"
            )
        if not isinstance(commit, str) or not COMMIT_RE.fullmatch(commit):
            raise ValueError(
                f"toolchain {toolchain_version!r} {project} commit must be a lowercase "
                "40-character SHA"
            )
        sources[project] = {
            "repository": repository,
            "commit": commit,
        }
    validate_source_set_toolchain(
        source_set.document,
        version=toolchain_version,
        toolchain=toolchain,
    )

    bases = matrix.get("bases")
    if not isinstance(bases, dict):
        raise ValueError("matrix bases must be an object")
    base_id = stream.get("base")
    if not isinstance(base_id, str):
        raise ValueError("stream base must be a string")
    base = bases.get(base_id)
    if not isinstance(base, dict):
        accepted = ", ".join(sorted(map(str, bases)))
        raise ValueError(f"unsupported base: {base_id}; accepted bases: {accepted}")
    if set(base) != BASE_KEYS:
        raise ValueError(
            f"base {base_id!r} keys must be exactly {sorted(BASE_KEYS)!r}"
        )
    for field in BASE_KEYS:
        if not isinstance(base.get(field), str) or not base[field]:
            raise ValueError(f"base {base_id!r} {field} must be a string")

    resolved_toolchain = copy.deepcopy(toolchain)
    for project in SOURCE_REPOSITORIES:
        resolved_toolchain[project]["version"] = toolchain_version
    resolved_base = {"id": base_id, **copy.deepcopy(base)}

    resolved = copy.deepcopy(stream)
    resolved.update(
        {
            "release_series": series,
            "release_branch": expected_branch,
            "source_set_id": source_set_id,
            "source_set": copy.deepcopy(source_set.document),
            "source_set_sha256": source_set.sha256,
            "toolchain_version": toolchain_version,
            "kolla_repository": sources["kolla"]["repository"],
            "kolla_version": toolchain_version,
            "kolla_commit": sources["kolla"]["commit"],
            "kolla_ansible_repository": sources["kolla_ansible"]["repository"],
            "kolla_ansible_version": toolchain_version,
            "kolla_ansible_commit": sources["kolla_ansible"]["commit"],
            "base_id": base_id,
            "distro": base["distro"],
            "os_version": base["os_version"],
            "base_image": base["image"],
            "base_tag": base["tag"],
            # Compatibility for selectors/callers during the v4 migration.
            "tag_token": base["os_version"],
            "toolchain": resolved_toolchain,
            "base": resolved_base,
        }
    )
    return resolved


def find_stream(matrix: dict[str, Any], stream_id: str) -> dict[str, Any]:
    matches = [
        stream
        for stream in matrix["streams"]
        if isinstance(stream, dict) and stream.get("id") == stream_id
    ]
    if len(matches) == 1:
        return resolve_stream(matrix, matches[0])
    if len(matches) > 1:
        raise ValueError(
            f"stream ID must identify exactly one stream: {stream_id}; "
            f"found {len(matches)}"
        )
    accepted = ", ".join(stream_ids(matrix))
    raise ValueError(f"unsupported stream: {stream_id}; accepted streams: {accepted}")


def load_profile(name: str, profiles_dir: Path = PROFILES_DIR) -> dict[str, Any]:
    path = profiles_dir / f"{name}.json"
    if not path.exists():
        raise ValueError(f"profile does not exist: {path.relative_to(ROOT)}")
    profile = load_json(path)
    if profile.get("name") != name:
        raise ValueError(f"profile name mismatch in {path.relative_to(ROOT)}")
    return profile


def selector_matches(
    applies_to: dict[str, list[str]] | None,
    stream: dict[str, Any],
) -> bool:
    if applies_to is None:
        return True
    unknown = set(applies_to) - set(SELECTOR_FIELDS)
    if unknown:
        raise ValueError(f"unsupported applies_to keys: {sorted(unknown)}")
    if not applies_to:
        raise ValueError("applies_to must not be empty")
    return all(
        stream[SELECTOR_FIELDS[field]] in accepted
        for field, accepted in applies_to.items()
    )


def resolve_profile(profile: dict[str, Any], stream: dict[str, Any]) -> dict[str, Any]:
    if profile.get("schema_version") != 3:
        raise ValueError(f"profile {profile.get('name')!r} schema_version must be 3")
    if stream["id"] not in profile.get("reviewed_streams", []):
        raise ValueError(
            f"profile {profile.get('name')!r} has not reviewed stream {stream['id']!r}"
        )
    resolved_images: list[dict[str, Any]] = []
    for raw_image in profile["images"]:
        if not selector_matches(raw_image.get("applies_to"), stream):
            continue
        variables: list[str] = []
        for raw_variable in raw_image["kolla_ansible_variables"]:
            if isinstance(raw_variable, str):
                variables.append(raw_variable)
            elif selector_matches(raw_variable.get("applies_to"), stream):
                variables.append(raw_variable["name"])
        image = copy.deepcopy(raw_image)
        image.pop("applies_to", None)
        image["kolla_ansible_variables"] = variables
        resolved_images.append(image)
    resolved_names = {image["name"] for image in resolved_images}
    resolved_groups: list[dict[str, Any]] = []
    for raw_group in profile["build_groups"]:
        if not selector_matches(raw_group.get("applies_to"), stream):
            continue
        images = [name for name in raw_group["images"] if name in resolved_names]
        if images:
            group = copy.deepcopy(raw_group)
            group.pop("applies_to", None)
            group["images"] = images
            resolved_groups.append(group)
    resolved = copy.deepcopy(profile)
    resolved["images"] = resolved_images
    resolved["build_groups"] = resolved_groups
    resolved["resolved_stream"] = stream["id"]
    return resolved


def validate_candidate_id(
    candidate_id: str,
    *,
    allow_local: bool = True,
) -> str:
    if type(candidate_id) is not str:
        raise ValueError("candidate ID must be a string")
    if allow_local and candidate_id == LOCAL_DRY_RUN_CANDIDATE_ID:
        return candidate_id
    if not CANDIDATE_ID_RE.fullmatch(candidate_id):
        expectation = "a workflow candidate ID <run_id>-<run_attempt>"
        if allow_local:
            expectation += f" or {LOCAL_DRY_RUN_CANDIDATE_ID!r}"
        raise ValueError(f"candidate ID must be {expectation}")
    return candidate_id


def render_tag(
    matrix: dict[str, Any],
    stream: dict[str, Any],
    arch: str | None = None,
) -> str:
    stream_tag = matrix["tag_policy"]["deploy_tag_template"].format(
        stream=stream["id"],
        release=stream["release"],
        distro=stream["distro"],
        base_tag=stream["base_tag"],
        tag_token=stream["tag_token"],
        os_version=stream["os_version"],
        kolla_ansible_version=stream["kolla_ansible_version"],
    )
    return f"{stream_tag}-{arch}" if arch else stream_tag


def render_revision_tag(
    matrix: dict[str, Any],
    stream: dict[str, Any],
    candidate_id: str,
    arch: str | None = None,
) -> str:
    """Render one immutable revision tag without a matrix-level template."""
    validate_candidate_id(candidate_id)
    suffix = f"-rev-{candidate_id}"
    if arch is not None:
        suffix += f"-{arch}"
    return f"{render_tag(matrix, stream)}{suffix}"
