"""Release-branch ownership policy for Kolla image publication."""

from __future__ import annotations

import re
from typing import Any

try:
    from scripts.base_resolution import validate_resolved_base
    from scripts.openstack_source_set import validate_frozen_source_contract
except ModuleNotFoundError:
    from base_resolution import validate_resolved_base
    from openstack_source_set import validate_frozen_source_contract


RELEASE_RE = re.compile(r"^(?P<year>[1-9][0-9]{3})\.(?P<cycle>[1-9][0-9]*)$")
RELEASE_BRANCH_RE = re.compile(
    r"^(?P<year>[1-9][0-9]{3})-(?P<cycle>[1-9][0-9]*)$"
)
HEADS_REF_PREFIX = "refs/heads/"


def release_branch_for(release: str) -> str:
    """Return the one repository branch allowed to own an OpenStack release."""
    if type(release) is not str:
        raise ValueError("OpenStack release must be a string in YYYY.N form")
    match = RELEASE_RE.fullmatch(release)
    if match is None:
        raise ValueError(f"invalid OpenStack release {release!r}; expected YYYY.N")
    return f"{match.group('year')}-{match.group('cycle')}"


def release_for_branch(branch_name: str) -> str:
    """Return the OpenStack release owned by an exact release branch name."""
    if type(branch_name) is not str:
        raise ValueError("release branch must be a string in YYYY-N form")
    match = RELEASE_BRANCH_RE.fullmatch(branch_name)
    if match is None:
        raise ValueError(f"invalid release branch {branch_name!r}; expected YYYY-N")
    return f"{match.group('year')}.{match.group('cycle')}"


def branch_for_ref(git_ref: str) -> str:
    """Return the canonical release branch carried by an exact heads ref."""
    if type(git_ref) is not str or not git_ref.startswith(HEADS_REF_PREFIX):
        raise ValueError(
            f"invalid release ref {git_ref!r}; expected refs/heads/YYYY-N"
        )
    branch_name = git_ref.removeprefix(HEADS_REF_PREFIX)
    release_for_branch(branch_name)
    return branch_name


def validate_matrix_branch(
    matrix: dict[str, Any],
    branch_name: str,
) -> list[str]:
    """Validate that a branch-local matrix contains only its owned release."""
    try:
        expected_release = release_for_branch(branch_name)
    except ValueError as error:
        return [str(error)]

    errors: list[str] = []
    streams = matrix.get("streams") if isinstance(matrix, dict) else None
    if not isinstance(streams, list) or not streams:
        errors.append("matrix streams must be a non-empty list")
    else:
        for index, stream in enumerate(streams):
            if not isinstance(stream, dict):
                errors.append(f"matrix streams[{index}] must be an object")
                continue
            stream_id = stream.get("id")
            release = stream.get("release")
            if release != expected_release:
                errors.append(
                    f"branch {branch_name!r} owns release {expected_release!r}, "
                    f"but matrix stream {stream_id!r} uses release {release!r}"
                )

    releases = matrix.get("releases") if isinstance(matrix, dict) else None
    if not isinstance(releases, dict):
        errors.append("matrix releases must be an object")
    elif set(releases) != {expected_release}:
        errors.append(
            f"branch {branch_name!r} must contain exactly the "
            f"{expected_release!r} release; got "
            f"{sorted(map(str, releases))!r}"
        )

    referenced_toolchains = {
        stream.get("toolchain")
        for stream in streams or []
        if isinstance(stream, dict) and isinstance(stream.get("toolchain"), str)
    }
    toolchains = matrix.get("toolchains") if isinstance(matrix, dict) else None
    if not isinstance(toolchains, dict):
        errors.append("matrix toolchains must be an object")
    elif set(toolchains) != referenced_toolchains:
        errors.append(
            f"branch {branch_name!r} toolchains must exactly match stream "
            f"references; expected {sorted(referenced_toolchains)!r}, got "
            f"{sorted(map(str, toolchains))!r}"
        )

    referenced_bases = {
        stream.get("base")
        for stream in streams or []
        if isinstance(stream, dict) and isinstance(stream.get("base"), str)
    }
    bases = matrix.get("bases") if isinstance(matrix, dict) else None
    if not isinstance(bases, dict):
        errors.append("matrix bases must be an object")
    elif set(bases) != referenced_bases:
        errors.append(
            f"branch {branch_name!r} bases must exactly match stream references; "
            f"expected {sorted(referenced_bases)!r}, got {sorted(map(str, bases))!r}"
        )

    return errors


def validate_plan_matrix(
    matrix: dict[str, Any],
    plan: dict[str, Any],
    branch_name: str,
) -> list[str]:
    """Bind a frozen plan to the one stream and toolchain owned by a branch."""
    errors = validate_matrix_branch(matrix, branch_name)
    try:
        expected_release = release_for_branch(branch_name)
    except ValueError as error:
        return [str(error)]

    if not isinstance(plan, dict):
        return [*errors, "publish plan must be an object"]

    if plan.get("release") != expected_release:
        errors.append(
            f"publish plan release must be {expected_release!r} for branch "
            f"{branch_name!r}, got {plan.get('release')!r}"
        )
    if plan.get("release_branch") != branch_name:
        errors.append(
            f"publish plan release_branch must be {branch_name!r}, got "
            f"{plan.get('release_branch')!r}"
        )

    streams = matrix.get("streams") if isinstance(matrix, dict) else None
    stream_id = plan.get("stream")
    matching_streams = (
        [stream for stream in streams if isinstance(stream, dict) and stream.get("id") == stream_id]
        if isinstance(streams, list)
        else []
    )
    if type(stream_id) is not str or not stream_id:
        errors.append("publish plan stream must be a non-empty string")
    elif len(matching_streams) != 1:
        errors.append(
            f"publish plan stream {stream_id!r} must identify exactly one matrix stream"
        )
    elif matching_streams[0].get("release") != expected_release:
        errors.append(
            f"publish plan stream {stream_id!r} is not owned by release "
            f"{expected_release!r}"
        )

    if len(matching_streams) == 1:
        try:
            try:
                from scripts.profile_resolver import resolve_stream
            except ModuleNotFoundError:
                from profile_resolver import resolve_stream
            resolved = resolve_stream(matrix, matching_streams[0])
        except (KeyError, TypeError, ValueError) as error:
            errors.append(f"cannot resolve branch matrix stream: {error}")
        else:
            if plan.get("release_series") != resolved["release_series"]:
                errors.append(
                    "publish plan release_series must match the branch matrix release"
                )
            expected_provenance = {
                "release_metadata": matrix.get("release_metadata"),
                "kolla": {
                    "repository": resolved["kolla_repository"],
                    "version": resolved["kolla_version"],
                    "commit": resolved["kolla_commit"],
                },
                "kolla_ansible": {
                    "repository": resolved["kolla_ansible_repository"],
                    "version": resolved["kolla_ansible_version"],
                    "commit": resolved["kolla_ansible_commit"],
                },
            }
            for key, expected in expected_provenance.items():
                actual = plan.get(key)
                if type(actual) is not dict or actual != expected:
                    errors.append(
                        f"publish plan {key} must exactly match the branch matrix pin"
                    )
            try:
                configured_base = {
                    "id": resolved["base_id"],
                    "distro": resolved["distro"],
                    "os_version": resolved["os_version"],
                    "image": resolved["base_image"],
                    "tag": resolved["base_tag"],
                }
                validate_resolved_base(configured_base, plan.get("base"))
            except (KeyError, TypeError, ValueError) as error:
                errors.append(
                    "publish plan base must be a valid frozen resolution of the "
                    f"branch matrix base: {error}"
                )
            try:
                source_contract = validate_frozen_source_contract(
                    plan.get("openstack_sources")
                )
                if (
                    source_contract["source_set"] != resolved["source_set"]
                    or source_contract["canonical_digest"]
                    != resolved["source_set_sha256"]
                ):
                    raise ValueError(
                        "source-set does not match the branch matrix stream"
                    )
            except (KeyError, TypeError, ValueError) as error:
                errors.append(
                    "publish plan OpenStack sources must exactly match the branch "
                    f"matrix source-set: {error}"
                )

    return errors


def validate_publish_context(
    matrix: dict[str, Any],
    plan: dict[str, Any],
    git_ref: str,
    *,
    require_protected: bool = False,
    ref_protected: bool | None = None,
) -> list[str]:
    """Validate branch ownership, frozen plan identity, and optional protection."""
    try:
        branch_name = branch_for_ref(git_ref)
    except ValueError as error:
        return [str(error)]

    errors = validate_plan_matrix(matrix, plan, branch_name)
    if require_protected and ref_protected is not True:
        errors.append(f"publish ref {git_ref!r} must be protected")
    return errors


def validate_publish_source(
    plan: dict[str, Any],
    git_ref: str,
    ref_protected: bool,
) -> list[str]:
    """Validate a protected Git ref against the release frozen in a plan."""
    errors: list[str] = []
    release = plan.get("release") if isinstance(plan, dict) else None
    try:
        expected_branch = release_branch_for(release)
    except ValueError as error:
        return [f"publish plan release is invalid: {error}"]

    expected_ref = f"{HEADS_REF_PREFIX}{expected_branch}"
    if type(git_ref) is not str or git_ref != expected_ref:
        errors.append(
            f"release {release!r} may publish only from {expected_ref!r}; "
            f"got {git_ref!r}"
        )
    if ref_protected is not True:
        errors.append(f"publish ref {git_ref!r} must be protected")
    return errors
