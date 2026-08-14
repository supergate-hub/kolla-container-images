#!/usr/bin/env python3
"""Validate kolla-container-images repository configuration."""

from __future__ import annotations

import argparse
import json
import re
import string
import sys
from pathlib import Path
from typing import Any

try:
    from scripts.frozen_sources import (
        DELIVERABLE_FILES,
        DELIVERABLE_PROJECTS,
        FrozenSourceError,
        parse_deliverable_pin,
        verify_exact_checkout,
    )
    from scripts.openstack_source_set import OpenStackSourceSetError, load_source_set
    from scripts.profile_resolver import (
        find_stream,
        reject_duplicate_keys,
        resolve_profile,
        stream_ids,
    )
    from scripts.release_policy import release_branch_for, validate_matrix_branch
except ModuleNotFoundError:
    from frozen_sources import (
        DELIVERABLE_FILES,
        DELIVERABLE_PROJECTS,
        FrozenSourceError,
        parse_deliverable_pin,
        verify_exact_checkout,
    )
    from openstack_source_set import OpenStackSourceSetError, load_source_set
    from profile_resolver import (
        find_stream,
        reject_duplicate_keys,
        resolve_profile,
        stream_ids,
    )
    from release_policy import release_branch_for, validate_matrix_branch


ROOT = Path(__file__).resolve().parents[1]
MATRIX_PATH = ROOT / "config" / "build-matrix.json"
PROFILES_DIR = ROOT / "config" / "profiles"
SOURCE_SETS_DIR = ROOT / "config" / "openstack-sources"

EXPECTED_IDENTITY = {
    "owner": "supergate-hub",
    "repository": "kolla-container-images",
    "registry": "ghcr.io",
}
EXPECTED_PROFILES = ["core", "deployment"]
MATRIX_KEYS = frozenset(
    {
        "schema_version",
        "owner",
        "repository",
        "registry",
        "profiles",
        "release_metadata",
        "releases",
        "toolchains",
        "bases",
        "streams",
        "architectures",
        "tag_policy",
    }
)
RELEASE_KEYS = frozenset({"series", "source_set"})
TOOLCHAIN_KEYS = frozenset({"kolla", "kolla_ansible"})
SOURCE_PIN_KEYS = frozenset({"repository", "commit"})
BASE_KEYS = frozenset({"distro", "os_version", "image", "tag"})
STREAM_KEYS = frozenset(
    {"id", "release", "toolchain", "base", "publish_enabled"}
)
RELEASE_METADATA_KEYS = frozenset({"repository", "commit"})
RELEASES_REPOSITORY = "https://opendev.org/openstack/releases"
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
VERSION_RE = re.compile(r"^[1-9][0-9]*\.[0-9]+\.[0-9]+(?:\.[0-9A-Za-z]+)*$")
RELEASE_CONFIG_RE = re.compile(r"^[a-z][a-z0-9-]*$")
BASE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9.-]*$")
SOURCE_SET_ID_RE = re.compile(r"^[a-z][a-z0-9-]*$")
EXPECTED_ARCHITECTURES = ["amd64", "arm64"]
EXPECTED_TAG_POLICY = {
    "deploy_tag_template": (
        "{release}-{distro}-{os_version}-{kolla_ansible_version}"
    ),
}
DEPLOY_TEMPLATE_FIELDS = {
    "release",
    "distro",
    "os_version",
    "kolla_ansible_version",
}
SELECTOR_FIELDS = {"streams": "id", "releases": "release", "distros": "distro"}
IMAGE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
BUILD_GROUP_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
KOLLA_IMAGE_VARIABLE_RE = re.compile(r"^[a-z0-9_]+_image_full$")

EXPECTED_CORE_IMAGES = {
    "glance-api",
    "heat-api",
    "heat-api-cfn",
    "heat-engine",
    "horizon",
    "keystone",
    "keystone-fernet",
    "keystone-ssh",
    "neutron-dhcp-agent",
    "neutron-l3-agent",
    "neutron-metadata-agent",
    "neutron-openvswitch-agent",
    "neutron-server",
    "nova-api",
    "nova-compute",
    "nova-conductor",
    "nova-libvirt",
    "nova-novncproxy",
    "nova-scheduler",
    "nova-ssh",
    "placement-api",
}
EXPECTED_DEPLOYMENT_COMMON_IMAGES = {
    "cinder-api",
    "cinder-backup",
    "cinder-scheduler",
    "cinder-volume",
    "cron",
    "fluentd",
    "glance-api",
    "grafana",
    "haproxy",
    "heat-api",
    "heat-api-cfn",
    "heat-engine",
    "horizon",
    "iscsid",
    "keepalived",
    "keystone",
    "keystone-fernet",
    "keystone-ssh",
    "kolla-toolbox",
    "manila-api",
    "manila-data",
    "manila-scheduler",
    "manila-share",
    "mariadb-server",
    "memcached",
    "neutron-metadata-agent",
    "neutron-server",
    "nova-api",
    "nova-compute",
    "nova-conductor",
    "nova-libvirt",
    "nova-novncproxy",
    "nova-scheduler",
    "nova-ssh",
    "octavia-api",
    "octavia-driver-agent",
    "octavia-health-manager",
    "octavia-housekeeping",
    "octavia-worker",
    "opensearch",
    "opensearch-dashboards",
    "openvswitch-db-server",
    "openvswitch-vswitchd",
    "ovn-controller",
    "ovn-nb-db-server",
    "ovn-northd",
    "ovn-sb-db-relay",
    "ovn-sb-db-server",
    "placement-api",
    "prometheus-alertmanager",
    "prometheus-blackbox-exporter",
    "prometheus-cadvisor",
    "prometheus-elasticsearch-exporter",
    "prometheus-libvirt-exporter",
    "prometheus-memcached-exporter",
    "prometheus-mysqld-exporter",
    "prometheus-node-exporter",
    "prometheus-openstack-exporter",
    "prometheus-server",
    "proxysql",
    "rabbitmq",
    "valkey-sentinel",
    "valkey-server",
}
EXPECTED_2026_DEPLOYMENT_IMAGES = {
    "prometheus-openstack-network-exporter",
    "prometheus-valkey-exporter",
}
NEUTRON_SERVER_ALIASES = [
    "neutron_rpc_server_image_full",
    "neutron_periodic_worker_image_full",
    "neutron_ovn_maintenance_worker_image_full",
]
CORE_VARIABLE_OVERRIDES = {
    "nova-conductor": [
        "nova_super_conductor_image_full",
        "nova_conductor_image_full",
    ],
}
DEPLOYMENT_VARIABLE_OVERRIDES = {
    "mariadb-server": ["mariadb_image_full"],
    "neutron-metadata-agent": [
        "neutron_metadata_agent_image_full",
        "neutron_ovn_metadata_agent_image_full",
    ],
    "nova-conductor": [
        "nova_conductor_image_full",
        "nova_super_conductor_image_full",
    ],
    "openvswitch-db-server": ["openvswitch_db_image_full"],
    "ovn-nb-db-server": ["ovn_nb_db_image_full"],
    "ovn-sb-db-server": ["ovn_sb_db_image_full"],
    "valkey-server": ["valkey_image_full"],
}
EXPECTED_CORE_PARENTS = [
    "base",
    "openstack-base",
    "keystone-base",
    "glance-base",
    "placement-base",
    "nova-base",
    "neutron-base",
    "heat-base",
]
EXPECTED_KEYSTONE_PARENTS = ["base", "openstack-base", "keystone-base"]
EXPECTED_OVN_RELAY_PARENTS = [
    "base",
    "openvswitch-base",
    "ovn-base",
    "ovn-sb-db-server",
]
EXPECTED_DEPLOYMENT_PARENTS = [
    "base",
    "openvswitch-base",
    "ovn-base",
    "openstack-base",
    "keystone-base",
    "glance-base",
    "placement-base",
    "nova-base",
    "neutron-base",
    "heat-base",
    "octavia-base",
    "prometheus-base",
    "valkey-base",
    "cinder-base",
    "manila-base",
]


def load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as file_obj:
        return json.load(file_obj, object_pairs_hook=reject_duplicate_keys)


def template_fields(template: str) -> set[str]:
    return {
        field_name
        for _, field_name, _, _ in string.Formatter().parse(template)
        if field_name
    }


def validate_release_metadata(matrix: dict[str, Any], errors: list[str]) -> None:
    metadata = matrix.get("release_metadata")
    if not isinstance(metadata, dict):
        errors.append("release_metadata must be an object")
        return
    if set(metadata) != RELEASE_METADATA_KEYS:
        errors.append(
            "release_metadata keys must be exactly "
            f"{sorted(RELEASE_METADATA_KEYS)!r}"
        )
    if metadata.get("repository") != RELEASES_REPOSITORY:
        errors.append(
            f"release_metadata.repository must be {RELEASES_REPOSITORY!r}"
        )
    if not isinstance(metadata.get("commit"), str) or not COMMIT_RE.fullmatch(
        metadata["commit"]
    ):
        errors.append(
            "release_metadata.commit must be a lowercase 40-character SHA"
        )


def validate_releases(
    matrix: dict[str, Any],
    stream_releases: set[str],
    errors: list[str],
) -> None:
    releases = matrix.get("releases")
    if not isinstance(releases, dict) or not releases:
        errors.append("releases must be a non-empty object")
        return
    if set(releases) != stream_releases:
        errors.append(
            "release keys must exactly match stream releases; "
            f"expected {sorted(stream_releases)!r}, got "
            f"{sorted(map(str, releases))!r}"
        )
    seen_source_sets: set[str] = set()
    for release, release_config in releases.items():
        context = f"releases[{release!r}]"
        try:
            release_branch_for(release)
        except ValueError as error:
            errors.append(f"{context} has invalid release: {error}")
        if not isinstance(release_config, dict):
            errors.append(f"{context} must be an object")
            continue
        if set(release_config) != RELEASE_KEYS:
            errors.append(
                f"{context} keys must be exactly {sorted(RELEASE_KEYS)!r}"
            )
        series = release_config.get("series")
        if not isinstance(series, str) or not RELEASE_CONFIG_RE.fullmatch(series):
            errors.append(f"{context}.series must be an OpenStack series name")
        source_set = release_config.get("source_set")
        if not isinstance(source_set, str) or not SOURCE_SET_ID_RE.fullmatch(source_set):
            errors.append(f"{context}.source_set must be a source-set ID")
        elif source_set in seen_source_sets:
            errors.append(f"duplicate release source_set: {source_set}")
        else:
            seen_source_sets.add(source_set)


def validate_toolchains(
    matrix: dict[str, Any],
    stream_toolchains: set[str],
    errors: list[str],
) -> None:
    toolchains = matrix.get("toolchains")
    if not isinstance(toolchains, dict) or not toolchains:
        errors.append("toolchains must be a non-empty object")
        return

    if set(toolchains) != stream_toolchains:
        errors.append(
            "toolchain keys must exactly match stream references; "
            f"expected {sorted(stream_toolchains)!r}, got "
            f"{sorted(map(str, toolchains))!r}"
        )

    for version, toolchain in toolchains.items():
        context = f"toolchains[{version!r}]"
        if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
            errors.append(f"{context} key must be a Kolla version")
        if not isinstance(toolchain, dict):
            errors.append(f"{context} must be an object")
            continue
        if set(toolchain) != TOOLCHAIN_KEYS:
            errors.append(
                f"{context} keys must be exactly {sorted(TOOLCHAIN_KEYS)!r}"
            )

        for project, expected_repository in (
            ("kolla", "https://opendev.org/openstack/kolla"),
            ("kolla_ansible", "https://opendev.org/openstack/kolla-ansible"),
        ):
            source = toolchain.get(project)
            source_context = f"{context}.{project}"
            if not isinstance(source, dict):
                errors.append(f"{source_context} must be an object")
                continue
            if set(source) != SOURCE_PIN_KEYS:
                errors.append(
                    f"{source_context} keys must be exactly "
                    f"{sorted(SOURCE_PIN_KEYS)!r}"
                )
            if source.get("repository") != expected_repository:
                errors.append(
                    f"{source_context}.repository must be {expected_repository!r}"
                )
            commit = source.get("commit")
            if not isinstance(commit, str) or not COMMIT_RE.fullmatch(commit):
                errors.append(
                    f"{source_context}.commit must be a lowercase 40-character SHA"
                )


def validate_release_metadata_toolchain_pins(
    matrix: dict[str, Any],
    checkout: Path,
    errors: list[str],
) -> None:
    """Prove every release/toolchain association against an exact metadata checkout."""
    metadata = matrix.get("release_metadata")
    if not isinstance(metadata, dict):
        return
    repository = metadata.get("repository")
    commit = metadata.get("commit")
    if not isinstance(repository, str) or not isinstance(commit, str):
        return
    try:
        verify_exact_checkout(
            checkout,
            repository=repository,
            commit=commit,
        )
    except FrozenSourceError as error:
        errors.append(f"release metadata checkout is invalid: {error}")
        return

    releases = matrix.get("releases")
    toolchains = matrix.get("toolchains")
    streams = matrix.get("streams")
    if not isinstance(releases, dict) or not isinstance(toolchains, dict):
        return
    if not isinstance(streams, list):
        return

    associations = {
        (stream.get("release"), stream.get("toolchain"))
        for stream in streams
        if isinstance(stream, dict)
        and isinstance(stream.get("release"), str)
        and isinstance(stream.get("toolchain"), str)
    }
    for release, version in sorted(associations):
        release_config = releases.get(release)
        toolchain = toolchains.get(version)
        if not isinstance(release_config, dict) or not isinstance(toolchain, dict):
            continue
        series = release_config.get("series")
        if not isinstance(series, str) or not RELEASE_CONFIG_RE.fullmatch(series):
            continue

        for project in sorted(DELIVERABLE_PROJECTS):
            source = toolchain.get(project)
            if not isinstance(source, dict):
                continue
            expected_commit = source.get("commit")
            if not isinstance(expected_commit, str) or not COMMIT_RE.fullmatch(
                expected_commit
            ):
                continue
            metadata_path = (
                checkout
                / "deliverables"
                / series
                / DELIVERABLE_FILES[project]
            )
            try:
                actual_commit = parse_deliverable_pin(
                    metadata_path,
                    expected_project=DELIVERABLE_PROJECTS[project],
                    expected_version=version,
                )
            except FrozenSourceError as error:
                errors.append(
                    f"release {release!r} toolchain {version!r} {project} cannot "
                    f"be proven from pinned OpenStack Releases metadata: {error}"
                )
                continue
            if actual_commit != expected_commit:
                errors.append(
                    f"toolchains[{version!r}].{project}.commit "
                    f"{expected_commit!r} does not match pinned OpenStack Releases "
                    f"metadata {actual_commit!r} for release {release!r}"
                )


def validate_bases(
    matrix: dict[str, Any],
    stream_bases: set[str],
    errors: list[str],
) -> None:
    bases = matrix.get("bases")
    if not isinstance(bases, dict) or not bases:
        errors.append("bases must be a non-empty object")
        return
    if set(bases) != stream_bases:
        errors.append(
            "base keys must exactly match stream references; "
            f"expected {sorted(stream_bases)!r}, got {sorted(map(str, bases))!r}"
        )
    for base_id, base in bases.items():
        context = f"bases[{base_id!r}]"
        if not isinstance(base_id, str) or not BASE_ID_RE.fullmatch(base_id):
            errors.append(f"{context} key must be a base ID")
        if not isinstance(base, dict):
            errors.append(f"{context} must be an object")
            continue
        if set(base) != BASE_KEYS:
            errors.append(f"{context} keys must be exactly {sorted(BASE_KEYS)!r}")
        for field in BASE_KEYS:
            value = base.get(field)
            if not isinstance(value, str) or not value:
                errors.append(f"{context}.{field} must be a non-empty string")
        if any(key in base for key in ("digest", "index_digest", "platform_digests")):
                errors.append(f"{context} must not pin image digests in raw config")


def validate_source_sets(
    matrix: dict[str, Any],
    errors: list[str],
    *,
    require_exact_files: bool,
) -> None:
    releases = matrix.get("releases")
    if not isinstance(releases, dict):
        return
    source_sets_dir = getattr(matrix, "source_sets_dir", SOURCE_SETS_DIR)
    referenced: set[str] = set()
    for release, release_config in releases.items():
        if not isinstance(release_config, dict):
            continue
        series = release_config.get("series")
        source_set_id = release_config.get("source_set")
        if not all(isinstance(value, str) for value in (release, series, source_set_id)):
            continue
        referenced.add(source_set_id)
        try:
            source_set = load_source_set(
                source_sets_dir / f"{source_set_id}.json",
                expected_id=source_set_id,
                expected_release=release,
                expected_series=series,
            )
            if source_set.document["schema_version"] != 3:
                errors.append(
                    f"release {release!r} active source-set schema_version must be 3"
                )
        except OpenStackSourceSetError as error:
            errors.append(f"release {release!r} source-set is invalid: {error}")
    try:
        present = {path.stem for path in source_sets_dir.glob("*.json")}
    except OSError as error:
        errors.append(f"cannot inspect source-set directory {source_sets_dir}: {error}")
        return
    missing = referenced - present
    if missing:
        errors.append(
            f"source-set files are missing release references: {sorted(missing)!r}"
        )
    if require_exact_files:
        for source_set_id in sorted(present):
            try:
                source_set = load_source_set(
                    source_sets_dir / f"{source_set_id}.json",
                    expected_id=source_set_id,
                )
            except OpenStackSourceSetError as error:
                errors.append(
                    f"repository source-set {source_set_id!r} is invalid: {error}"
                )
                continue
            release = source_set.document["release"]
            release_config = releases.get(release)
            if not isinstance(release_config, dict):
                errors.append(
                    f"source-set {source_set_id!r} belongs to release {release!r}, "
                    "which is not owned by this matrix"
                )
                continue
            if source_set.document["series"] != release_config.get("series"):
                errors.append(
                    f"source-set {source_set_id!r} series does not match release "
                    f"{release!r}"
                )


def validate_matrix(
    matrix: dict[str, Any],
    errors: list[str],
    *,
    branch_name: str | None = None,
    require_exact_source_files: bool | None = None,
) -> None:
    if matrix.get("schema_version") != 4:
        errors.append("matrix schema_version must be 4")

    if set(matrix) != MATRIX_KEYS:
        errors.append(f"matrix keys must be exactly {sorted(MATRIX_KEYS)!r}")

    for field, expected in EXPECTED_IDENTITY.items():
        if matrix.get(field) != expected:
            errors.append(f"matrix {field} must be {expected!r}")

    if matrix.get("profiles") != EXPECTED_PROFILES:
        errors.append(f"profiles must be exactly {EXPECTED_PROFILES!r}")

    streams = matrix.get("streams")
    valid_stream_objects = isinstance(streams, list)
    if not isinstance(streams, list) or not streams:
        errors.append("streams must be a non-empty list")
        streams = []
        valid_stream_objects = False

    seen_ids: set[str] = set()
    seen_combinations: set[tuple[str, str, str]] = set()
    for index, stream in enumerate(streams):
        context = f"streams[{index}]"
        if not isinstance(stream, dict):
            errors.append(f"{context} must be an object")
            valid_stream_objects = False
            continue
        if set(stream) != STREAM_KEYS:
            errors.append(f"{context} keys must be exactly {sorted(STREAM_KEYS)!r}")

        stream_id = stream.get("id")
        if not isinstance(stream_id, str) or not stream_id:
            errors.append(f"{context}.id must be a non-empty string")
            valid_stream_objects = False
            continue
        if stream_id in seen_ids:
            errors.append(f"duplicate stream id: {stream_id}")
        seen_ids.add(stream_id)

        for field in ("release", "toolchain", "base"):
            value = stream.get(field)
            if not isinstance(value, str) or not value:
                errors.append(f"{context}.{field} must be a non-empty string")
        combination = tuple(stream.get(field) for field in ("release", "toolchain", "base"))
        if all(isinstance(value, str) for value in combination):
            if combination in seen_combinations:
                errors.append(f"duplicate stream combination: {combination!r}")
            seen_combinations.add(combination)

        publish_enabled = stream.get("publish_enabled")
        if not isinstance(publish_enabled, bool):
            errors.append(f"stream {stream_id!r} publish_enabled must be a boolean")

    ids: list[str] = []
    if valid_stream_objects:
        ids = stream_ids(matrix)

    stream_releases = {
        stream["release"]
        for stream in streams
        if isinstance(stream, dict) and isinstance(stream.get("release"), str)
    }
    stream_toolchains = {
        stream["toolchain"]
        for stream in streams
        if isinstance(stream, dict) and isinstance(stream.get("toolchain"), str)
    }
    stream_bases = {
        stream["base"]
        for stream in streams
        if isinstance(stream, dict) and isinstance(stream.get("base"), str)
    }
    validate_release_metadata(matrix, errors)
    validate_releases(matrix, stream_releases, errors)
    validate_source_sets(
        matrix,
        errors,
        require_exact_files=(
            branch_name in (None, "main")
            if require_exact_source_files is None
            else require_exact_source_files
        ),
    )
    validate_toolchains(matrix, stream_toolchains, errors)
    validate_bases(matrix, stream_bases, errors)
    if branch_name not in (None, "main"):
        errors.extend(validate_matrix_branch(matrix, branch_name))

    if matrix.get("architectures") != EXPECTED_ARCHITECTURES:
        errors.append(
            f"architectures must be exactly {EXPECTED_ARCHITECTURES!r}"
        )

    tag_policy = matrix.get("tag_policy")
    if not isinstance(tag_policy, dict):
        errors.append("tag_policy must be an object")
        return
    if set(tag_policy) != set(EXPECTED_TAG_POLICY):
        errors.append(
            f"tag_policy keys must be exactly {sorted(EXPECTED_TAG_POLICY)!r}"
        )

    deploy_template = tag_policy.get("deploy_tag_template")
    templates = {
        "deploy_tag_template": (
            deploy_template,
            DEPLOY_TEMPLATE_FIELDS,
        ),
    }
    for name, (template, expected_fields) in templates.items():
        if not isinstance(template, str):
            errors.append(f"tag_policy.{name} must be a string")
            continue
        try:
            actual_fields = template_fields(template)
        except ValueError as error:
            errors.append(f"invalid tag template {name}: {error}")
            continue
        if actual_fields != expected_fields:
            errors.append(
                f"{name} fields must be exactly {sorted(expected_fields)!r}"
            )
    if any(not isinstance(value[0], str) for value in templates.values()):
        return
    if tag_policy != EXPECTED_TAG_POLICY:
        errors.append(f"tag_policy must be exactly {EXPECTED_TAG_POLICY!r}")

    rendered_tags: set[str] = set()
    for stream in streams:
        if not isinstance(stream, dict) or not isinstance(stream.get("id"), str):
            continue
        stream_id = stream["id"]
        try:
            resolved_stream = find_stream(matrix, stream_id)
            deploy_tag = deploy_template.format(**resolved_stream)
            if deploy_tag != stream_id:
                errors.append(
                    f"stream id {stream_id!r} must equal its semantic deploy tag "
                    f"{deploy_tag!r}"
                )
            if deploy_tag in rendered_tags:
                errors.append(f"duplicate rendered deploy tag: {deploy_tag}")
            rendered_tags.add(deploy_tag)
            for arch in EXPECTED_ARCHITECTURES:
                arch_tag = f"{deploy_tag}-{arch}"
                expected = f"{stream_id}-{arch}"
                if arch_tag != expected:
                    errors.append(
                        f"architecture tag for {stream_id!r}/{arch!r} "
                        f"must be {expected!r}"
                    )
        except (AttributeError, IndexError, KeyError, TypeError, ValueError) as error:
            errors.append(f"cannot render tags for stream {stream_id!r}: {error}")


def validate_selector(
    selector: Any,
    matrix: dict[str, Any],
    context: str,
    errors: list[str],
) -> None:
    if not isinstance(selector, dict):
        errors.append(f"{context} must be an object")
        return
    if not selector:
        errors.append(f"{context} must not be empty")
        return

    unknown = set(selector) - set(SELECTOR_FIELDS)
    if unknown:
        errors.append(f"{context} has unsupported keys: {sorted(unknown)!r}")

    # Profiles are shared by every release branch, so values that are dormant
    # in a branch-local matrix remain syntactically valid. Cross-field matching
    # is enforced whenever every selected value is active in this matrix.
    stream_objects: list[dict[str, Any]] = []
    streams = matrix.get("streams")
    if isinstance(streams, list):
        for raw_stream in streams:
            if not isinstance(raw_stream, dict) or not isinstance(
                raw_stream.get("id"), str
            ):
                continue
            try:
                stream_objects.append(find_stream(matrix, raw_stream["id"]))
            except (KeyError, TypeError, ValueError):
                continue
    accepted = {
        field: {
            stream.get(stream_field)
            for stream in stream_objects
            if isinstance(stream.get(stream_field), str)
        }
        for field, stream_field in SELECTOR_FIELDS.items()
    }

    selector_is_valid = not unknown
    releases = matrix.get("releases")
    aggregate_catalog = isinstance(releases, dict) and len(releases) > 1
    for field, values in selector.items():
        if field not in SELECTOR_FIELDS:
            continue
        field_context = f"{context}.{field}"
        if not isinstance(values, list) or not values:
            errors.append(f"{field_context} must be a non-empty list")
            selector_is_valid = False
            continue
        for value in values:
            if not isinstance(value, str) or not value:
                errors.append(f"{field_context} values must be non-empty strings")
                selector_is_valid = False
            elif aggregate_catalog and value not in accepted[field]:
                errors.append(
                    f"{field_context} contains unsupported value: {value!r}"
                )
                selector_is_valid = False

    selector_is_active = selector_is_valid and all(
        set(values) <= accepted[field]
        for field, values in selector.items()
        if field in SELECTOR_FIELDS and isinstance(values, list)
    )
    if selector_is_active and not any(
        all(
            stream.get(SELECTOR_FIELDS[field]) in values
            for field, values in selector.items()
        )
        for stream in stream_objects
    ):
        errors.append(f"{context} does not match any supported stream")


def expected_resolved_images(
    profile_name: str, stream: dict[str, Any]
) -> set[str]:
    if profile_name == "core":
        return set(EXPECTED_CORE_IMAGES)
    expected = set(EXPECTED_DEPLOYMENT_COMMON_IMAGES)
    if stream["distro"] == "ubuntu":
        expected.add("tgtd")
    if stream["release"] == "2026.1":
        expected.update(EXPECTED_2026_DEPLOYMENT_IMAGES)
    return expected


def expected_image_variables(
    profile_name: str,
    image_name: str,
    release: str,
) -> list[str]:
    overrides = (
        CORE_VARIABLE_OVERRIDES
        if profile_name == "core"
        else DEPLOYMENT_VARIABLE_OVERRIDES
    )
    variables = list(
        overrides.get(
            image_name,
            [f"{image_name.replace('-', '_')}_image_full"],
        )
    )
    if image_name == "neutron-server" and release in {"2025.2", "2026.1"}:
        variables.extend(NEUTRON_SERVER_ALIASES)
    return variables


def resolved_parent_sequence(
    build_groups: list[dict[str, Any]],
    selected_images: set[str] | None = None,
) -> list[str]:
    if selected_images is None:
        selected_images = {
            image
            for group in build_groups
            if isinstance(group.get("images"), list)
            for image in group["images"]
            if isinstance(image, str)
        }

    parents: list[str] = []
    for group in build_groups:
        group_images = group.get("images")
        if not isinstance(group_images, list):
            continue
        if selected_images.isdisjoint(group_images):
            continue
        group_parents = group.get("parents")
        if group_parents is None:
            parent = group.get("parent")
            group_parents = list(
                dict.fromkeys(["base", "openstack-base", parent])
            )
        if not isinstance(group_parents, list):
            continue
        for parent in group_parents:
            if (
                isinstance(parent, str)
                and parent not in selected_images
                and parent not in parents
            ):
                parents.append(parent)
    return parents


def validate_resolved_policy(
    profile_name: str,
    stream: dict[str, Any],
    profile: dict[str, Any],
    errors: list[str],
) -> None:
    stream_id = stream["id"]
    context = f"config/profiles/{profile_name}.json resolved for {stream_id!r}"
    images = profile.get("images")
    if not isinstance(images, list) or not all(
        isinstance(image, dict) and isinstance(image.get("name"), str)
        for image in images
    ):
        return

    images_by_name = {image["name"]: image for image in images}
    expected_names = expected_resolved_images(profile_name, stream)
    actual_names = set(images_by_name)
    if actual_names != expected_names:
        errors.append(
            f"{context} resolved image set must be exactly "
            f"{sorted(expected_names)!r}; got {sorted(actual_names)!r}"
        )

    for image_name in sorted(actual_names & expected_names):
        expected_variables = expected_image_variables(
            profile_name, image_name, stream["release"]
        )
        actual_variables = images_by_name[image_name].get(
            "kolla_ansible_variables"
        )
        if actual_variables != expected_variables:
            errors.append(
                f"{context} {image_name} variable mapping must be exactly "
                f"{expected_variables!r}; got {actual_variables!r}"
            )

    build_groups = profile.get("build_groups")
    if not isinstance(build_groups, list) or not all(
        isinstance(group, dict) for group in build_groups
    ):
        return
    expected_parents = (
        EXPECTED_CORE_PARENTS
        if profile_name == "core"
        else EXPECTED_DEPLOYMENT_PARENTS
    )
    if profile_name == "deployment" and stream["release"] == "2025.1":
        expected_parents = [
            *EXPECTED_DEPLOYMENT_PARENTS[:11],
            "mariadb-base",
            *EXPECTED_DEPLOYMENT_PARENTS[11:],
        ]
    actual_parents = resolved_parent_sequence(build_groups, actual_names)
    if actual_parents != expected_parents:
        errors.append(
            f"{context} resolved parent set must be exactly "
            f"{expected_parents!r}; got {actual_parents!r}"
        )

    if profile_name == "core":
        keystone_parents = resolved_parent_sequence(
            build_groups, {"keystone"}
        )
        if keystone_parents != EXPECTED_KEYSTONE_PARENTS:
            errors.append(
                f"{context} core/keystone resolved parent set must be exactly "
                f"{EXPECTED_KEYSTONE_PARENTS!r}; got {keystone_parents!r}"
            )
    elif profile_name == "deployment":
        relay_groups = [
            group
            for group in build_groups
            if "ovn-sb-db-relay" in group.get("images", [])
        ]
        relay_parents = (
            relay_groups[0].get("parents") if len(relay_groups) == 1 else None
        )
        if relay_parents != EXPECTED_OVN_RELAY_PARENTS:
            errors.append(
                f"{context} ovn-sb-db-relay parent chain must be exactly "
                f"{EXPECTED_OVN_RELAY_PARENTS!r}; got {relay_parents!r}"
            )


def validate_resolved_profile(
    profile_name: str,
    stream: dict[str, Any],
    profile: dict[str, Any],
    errors: list[str],
) -> None:
    stream_id = stream["id"]
    context = f"config/profiles/{profile_name}.json resolved for {stream_id!r}"
    if profile.get("resolved_stream") != stream_id:
        errors.append(f"{context} resolved_stream must be {stream_id!r}")

    images = profile.get("images")
    if not isinstance(images, list) or not images:
        errors.append(f"{context} images must be a non-empty list")
        return

    image_names: set[str] = set()
    kolla_variables: set[str] = set()
    for index, image in enumerate(images):
        if not isinstance(image, dict):
            errors.append(f"{context} images[{index}] must be an object")
            continue
        if "applies_to" in image:
            errors.append(f"{context} images[{index}] must be fully resolved")

        name = image.get("name")
        if not isinstance(name, str):
            errors.append(f"{context} images[{index}].name must be a string")
        elif name in image_names:
            errors.append(f"{context} duplicate image name: {name}")
        else:
            image_names.add(name)

        variables = image.get("kolla_ansible_variables")
        if not isinstance(variables, list) or not variables:
            errors.append(
                f"{context} images[{index}].kolla_ansible_variables "
                "must be a non-empty list"
            )
            continue
        for variable in variables:
            if not isinstance(variable, str) or not KOLLA_IMAGE_VARIABLE_RE.fullmatch(
                variable
            ):
                errors.append(
                    f"{context} images[{index}] has unresolved or invalid "
                    f"Kolla-Ansible variable: {variable!r}"
                )
            elif variable in kolla_variables:
                errors.append(
                    f"{context} duplicate Kolla-Ansible variable: {variable}"
                )
            else:
                kolla_variables.add(variable)

    build_groups = profile.get("build_groups")
    if not isinstance(build_groups, list) or not build_groups:
        errors.append(f"{context} build_groups must be a non-empty list")
        return

    build_group_names: set[str] = set()
    grouped_images: set[str] = set()
    for index, build_group in enumerate(build_groups):
        if not isinstance(build_group, dict):
            errors.append(f"{context} build_groups[{index}] must be an object")
            continue
        if "applies_to" in build_group:
            errors.append(
                f"{context} build_groups[{index}] must be fully resolved"
            )
        group_name = build_group.get("name")
        if not isinstance(group_name, str):
            errors.append(
                f"{context} build_groups[{index}].name must be a string"
            )
        elif group_name in build_group_names:
            errors.append(f"{context} duplicate resolved build group: {group_name}")
        else:
            build_group_names.add(group_name)

        group_images = build_group.get("images")
        if not isinstance(group_images, list) or not group_images:
            errors.append(
                f"{context} build_groups[{index}].images must be a non-empty list"
            )
            continue
        for image in group_images:
            if not isinstance(image, str) or image not in image_names:
                errors.append(
                    f"{context} build_groups[{index}] references unknown image: {image!r}"
                )
            elif image in grouped_images:
                errors.append(
                    f"{context} image appears in multiple build groups: {image}"
                )
            else:
                grouped_images.add(image)

    for image in sorted(image_names - grouped_images):
        errors.append(f"{context} image is not assigned to a build group: {image}")

    validate_resolved_policy(profile_name, stream, profile, errors)


def validate_profile(
    matrix: dict[str, Any],
    profile_name: str,
    profile: dict[str, Any],
    errors: list[str],
) -> None:
    context = f"config/profiles/{profile_name}.json"
    if profile.get("schema_version") != 3:
        errors.append(f"{context} schema_version must be 3")
    if profile.get("name") != profile_name:
        errors.append(f"{context} name must be {profile_name!r}")

    reviewed_streams = profile.get("reviewed_streams")
    if (
        not isinstance(reviewed_streams, list)
        or not reviewed_streams
        or not all(isinstance(stream_id, str) and stream_id for stream_id in reviewed_streams)
    ):
        errors.append(f"{context} reviewed_streams must be a non-empty string list")
    else:
        if len(reviewed_streams) != len(set(reviewed_streams)):
            errors.append(f"{context} reviewed_streams must not contain duplicates")
        active_streams = set(stream_ids(matrix))
        if not active_streams <= set(reviewed_streams):
            errors.append(
                f"{context} reviewed_streams must include every active stream: "
                f"{sorted(active_streams)!r}"
            )
        releases = matrix.get("releases")
        if (
            isinstance(releases, dict)
            and len(releases) > 1
            and set(reviewed_streams) != active_streams
        ):
            errors.append(
                f"{context} aggregate reviewed_streams must exactly match "
                f"{sorted(active_streams)!r}"
            )

    images = profile.get("images")
    if not isinstance(images, list) or not images:
        errors.append(f"{context} images must be a non-empty list")
        images = []

    image_names: set[str] = set()
    kolla_variables: set[str] = set()
    for index, image in enumerate(images):
        image_context = f"{context} images[{index}]"
        if not isinstance(image, dict):
            errors.append(f"{image_context} must be an object")
            continue

        name = image.get("name")
        if not isinstance(name, str) or not IMAGE_NAME_RE.fullmatch(name):
            errors.append(f"{image_context}.name must be a Kolla image name")
        elif name in image_names:
            errors.append(f"{context} duplicate image name: {name}")
        else:
            image_names.add(name)

        if "applies_to" in image:
            validate_selector(
                image["applies_to"], matrix, f"{image_context}.applies_to", errors
            )

        variables = image.get("kolla_ansible_variables")
        if not isinstance(variables, list) or not variables:
            errors.append(
                f"{image_context}.kolla_ansible_variables must be a non-empty list"
            )
            continue

        for variable_index, raw_variable in enumerate(variables):
            variable_context = (
                f"{image_context}.kolla_ansible_variables[{variable_index}]"
            )
            if isinstance(raw_variable, str):
                variable = raw_variable
            elif isinstance(raw_variable, dict):
                if set(raw_variable) != {"name", "applies_to"}:
                    errors.append(
                        f"{variable_context} keys must be exactly ['applies_to', 'name']"
                    )
                variable = raw_variable.get("name")
                validate_selector(
                    raw_variable.get("applies_to"),
                    matrix,
                    f"{variable_context}.applies_to",
                    errors,
                )
            else:
                errors.append(
                    f"{variable_context} must be a variable name or selector object"
                )
                continue

            if not isinstance(variable, str) or not KOLLA_IMAGE_VARIABLE_RE.fullmatch(
                variable
            ):
                errors.append(
                    f"{variable_context} has invalid Kolla-Ansible variable: {variable!r}"
                )
            elif variable in kolla_variables:
                errors.append(
                    f"{context} duplicate Kolla-Ansible variable: {variable}"
                )
            else:
                kolla_variables.add(variable)

    build_groups = profile.get("build_groups")
    if not isinstance(build_groups, list) or not build_groups:
        errors.append(f"{context} build_groups must be a non-empty list")
        build_groups = []

    build_group_names: set[str] = set()
    grouped_images: set[str] = set()
    for index, build_group in enumerate(build_groups):
        group_context = f"{context} build_groups[{index}]"
        if not isinstance(build_group, dict):
            errors.append(f"{group_context} must be an object")
            continue

        group_name = build_group.get("name")
        if not isinstance(group_name, str) or not BUILD_GROUP_NAME_RE.fullmatch(
            group_name
        ):
            errors.append(f"{group_context}.name must be a build group name")
        elif group_name in build_group_names:
            errors.append(f"{context} duplicate build group: {group_name}")
        else:
            build_group_names.add(group_name)

        if "applies_to" in build_group:
            validate_selector(
                build_group["applies_to"],
                matrix,
                f"{group_context}.applies_to",
                errors,
            )

        parent = build_group.get("parent")
        if not isinstance(parent, str) or not IMAGE_NAME_RE.fullmatch(parent):
            errors.append(f"{group_context}.parent must be a Kolla image name")

        parents = build_group.get("parents")
        if parents is not None:
            if not isinstance(parents, list) or not parents:
                errors.append(f"{group_context}.parents must be a non-empty list")
            else:
                seen_parents: set[str] = set()
                for chain_parent in parents:
                    if not isinstance(chain_parent, str) or not IMAGE_NAME_RE.fullmatch(
                        chain_parent
                    ):
                        errors.append(
                            f"{group_context} has invalid parent chain image: "
                            f"{chain_parent!r}"
                        )
                    elif chain_parent in seen_parents:
                        errors.append(
                            f"{group_context} duplicates parent chain image: {chain_parent}"
                        )
                    else:
                        seen_parents.add(chain_parent)
                if isinstance(parent, str) and parents[-1] != parent:
                    errors.append(
                        f"{group_context}.parents must end with build_groups[].parent"
                    )

        group_images = build_group.get("images")
        if not isinstance(group_images, list) or not group_images:
            errors.append(f"{group_context}.images must be a non-empty list")
            continue
        for image in group_images:
            if not isinstance(image, str) or image not in image_names:
                errors.append(
                    f"{context} build group {group_name!r} references unknown image: "
                    f"{image!r}"
                )
            else:
                grouped_images.add(image)

    for image in sorted(image_names - grouped_images):
        errors.append(f"{context} image is not assigned to a build group: {image}")

    streams = matrix.get("streams")
    if not isinstance(streams, list) or not all(
        isinstance(stream, dict) and isinstance(stream.get("id"), str)
        for stream in streams
    ):
        return
    for stream_id in stream_ids(matrix):
        try:
            stream = find_stream(matrix, stream_id)
            resolved = resolve_profile(profile, stream)
        except ValueError as error:
            errors.append(f"{context}: {error}")
            continue
        except (AttributeError, KeyError, TypeError) as error:
            errors.append(f"{context} could not resolve stream {stream_id!r}: {error}")
            continue
        validate_resolved_profile(profile_name, stream, resolved, errors)


def validate_profiles(matrix: dict[str, Any], errors: list[str]) -> None:
    profiles = matrix.get("profiles")
    if not isinstance(profiles, list) or not profiles:
        errors.append("profiles must be a non-empty list")
        return

    for profile_name in profiles:
        if not isinstance(profile_name, str) or not profile_name:
            errors.append(f"profile reference must be a non-empty string: {profile_name!r}")
            continue
        profile_path = PROFILES_DIR / f"{profile_name}.json"
        if not profile_path.exists():
            errors.append(f"profile does not exist: {profile_path.relative_to(ROOT)}")
            continue
        try:
            profile = load_json(profile_path)
        except (OSError, UnicodeError, ValueError) as error:
            errors.append(
                f"cannot read {profile_path.relative_to(ROOT)}: {error}"
            )
            continue
        if not isinstance(profile, dict):
            errors.append(f"{profile_path.relative_to(ROOT)} must contain an object")
            continue
        validate_profile(matrix, profile_name, profile, errors)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate Kolla image repository configuration.",
        allow_abbrev=False,
    )
    parser.add_argument(
        "--branch",
        help="Optional target branch context: main or an exact YYYY-N release branch",
    )
    parser.add_argument(
        "--release-metadata-checkout",
        type=Path,
        help=(
            "Optional exact, detached OpenStack Releases checkout used to prove "
            "matrix Kolla and Kolla-Ansible pins"
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    errors: list[str] = []
    try:
        matrix = load_json(MATRIX_PATH)
    except (OSError, UnicodeError, ValueError) as error:
        errors.append(f"cannot read {MATRIX_PATH.relative_to(ROOT)}: {error}")
    else:
        if not isinstance(matrix, dict):
            errors.append(f"{MATRIX_PATH.relative_to(ROOT)} must contain an object")
        else:
            validate_matrix(
                matrix,
                errors,
                branch_name=args.branch,
                require_exact_source_files=True,
            )
            if args.release_metadata_checkout is not None:
                validate_release_metadata_toolchain_pins(
                    matrix,
                    args.release_metadata_checkout,
                    errors,
                )
            validate_profiles(matrix, errors)

    if errors:
        print("Configuration validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    print("Configuration validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
