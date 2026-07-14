from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
MATRIX_PATH = ROOT / "config" / "build-matrix.json"
PROFILES_DIR = ROOT / "config" / "profiles"
SELECTOR_FIELDS = {"streams": "id", "releases": "release", "distros": "distro"}
LOCAL_DRY_RUN_CANDIDATE_ID = "local-dry-run"
CANDIDATE_ID_RE = re.compile(r"^[1-9][0-9]*-[1-9][0-9]*$")


def load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as file_obj:
        return json.load(file_obj)


def load_matrix(path: Path = MATRIX_PATH) -> dict[str, Any]:
    return load_json(path)


def stream_ids(matrix: dict[str, Any]) -> list[str]:
    return [stream["id"] for stream in matrix["streams"]]


def find_stream(matrix: dict[str, Any], stream_id: str) -> dict[str, Any]:
    for stream in matrix["streams"]:
        if stream["id"] == stream_id:
            return stream
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
    )
    return f"{stream_tag}-{arch}" if arch else stream_tag


def render_candidate_tag(
    matrix: dict[str, Any],
    stream: dict[str, Any],
    candidate_id: str,
    arch: str | None = None,
) -> str:
    candidate_id = validate_candidate_id(candidate_id)
    template_name = (
        "candidate_arch_tag_template" if arch else "candidate_tag_template"
    )
    return matrix["tag_policy"][template_name].format(
        stream=stream["id"],
        release=stream["release"],
        distro=stream["distro"],
        base_tag=stream["base_tag"],
        tag_token=stream["tag_token"],
        candidate_id=candidate_id,
        arch=arch or "",
    )
