#!/usr/bin/env python3
"""Validate kolla-container-images repository configuration."""

from __future__ import annotations

import json
import re
import string
import sys
from pathlib import Path
from typing import Any

try:
    from scripts.profile_resolver import find_stream, resolve_profile, stream_ids
except ModuleNotFoundError:
    from profile_resolver import find_stream, resolve_profile, stream_ids


ROOT = Path(__file__).resolve().parents[1]
MATRIX_PATH = ROOT / "config" / "build-matrix.json"
PROFILES_DIR = ROOT / "config" / "profiles"

EXPECTED_IDENTITY = {
    "owner": "supergate-hub",
    "repository": "kolla-container-images",
    "registry": "ghcr.io",
}
EXPECTED_PROFILES = ["core", "deployment"]
EXPECTED_STREAMS = {
    "2025.1-rocky-9": ("2025.1", "20.4.0", "20.4.0", "rocky", "9", "9"),
    "2025.1-rocky-10": ("2025.1", "20.4.0", "20.4.0", "rocky", "10", "10"),
    "2025.1-ubuntu-noble": (
        "2025.1",
        "20.4.0",
        "20.4.0",
        "ubuntu",
        "24.04",
        "noble",
    ),
    "2025.2-rocky-10": ("2025.2", "21.1.0", "21.1.0", "rocky", "10", "10"),
    "2025.2-ubuntu-noble": (
        "2025.2",
        "21.1.0",
        "21.1.0",
        "ubuntu",
        "24.04",
        "noble",
    ),
    "2026.1-rocky-10": ("2026.1", "22.0.0", "22.0.0", "rocky", "10", "10"),
    "2026.1-ubuntu-noble": (
        "2026.1",
        "22.0.0",
        "22.0.0",
        "ubuntu",
        "24.04",
        "noble",
    ),
}
STREAM_FIELDS = (
    "release",
    "kolla_version",
    "kolla_ansible_version",
    "distro",
    "base_tag",
    "tag_token",
)
EXPECTED_ARCHITECTURES = ["amd64", "arm64"]
EXPECTED_TAG_POLICY = {
    "deploy_tag_template": "{release}-{distro}-{tag_token}",
    "candidate_tag_template": (
        "{release}-{distro}-{tag_token}-candidate-{candidate_id}"
    ),
    "candidate_arch_tag_template": (
        "{release}-{distro}-{tag_token}-candidate-{candidate_id}-{arch}"
    ),
}
DEPLOY_TEMPLATE_FIELDS = {"release", "distro", "tag_token"}
CANDIDATE_TEMPLATE_FIELDS = DEPLOY_TEMPLATE_FIELDS | {"candidate_id"}
CANDIDATE_ARCH_TEMPLATE_FIELDS = CANDIDATE_TEMPLATE_FIELDS | {"arch"}
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
        return json.load(file_obj)


def template_fields(template: str) -> set[str]:
    return {
        field_name
        for _, field_name, _, _ in string.Formatter().parse(template)
        if field_name
    }


def validate_matrix(matrix: dict[str, Any], errors: list[str]) -> None:
    if matrix.get("schema_version") != 2:
        errors.append("matrix schema_version must be 2")

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
    for index, stream in enumerate(streams):
        context = f"streams[{index}]"
        if not isinstance(stream, dict):
            errors.append(f"{context} must be an object")
            valid_stream_objects = False
            continue

        stream_id = stream.get("id")
        if not isinstance(stream_id, str) or not stream_id:
            errors.append(f"{context}.id must be a non-empty string")
            valid_stream_objects = False
            continue
        if stream_id in seen_ids:
            errors.append(f"duplicate stream id: {stream_id}")
        seen_ids.add(stream_id)

        expected = EXPECTED_STREAMS.get(stream_id)
        if expected is None:
            errors.append(f"unsupported stream id: {stream_id}")
        else:
            actual = tuple(stream.get(field) for field in STREAM_FIELDS)
            if actual != expected:
                errors.append(
                    f"stream {stream_id!r} fields {STREAM_FIELDS!r} "
                    f"must be {expected!r}"
                )

        publish_enabled = stream.get("publish_enabled")
        if not isinstance(publish_enabled, bool):
            errors.append(f"stream {stream_id!r} publish_enabled must be a boolean")
        elif publish_enabled is not True:
            errors.append(f"stream {stream_id!r} publish_enabled must be true")

    if valid_stream_objects:
        ids = stream_ids(matrix)
        if ids != list(EXPECTED_STREAMS):
            errors.append(f"stream IDs must be exactly {list(EXPECTED_STREAMS)!r}")

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
    candidate_template = tag_policy.get("candidate_tag_template")
    candidate_arch_template = tag_policy.get("candidate_arch_tag_template")
    templates = {
        "deploy_tag_template": (
            deploy_template,
            DEPLOY_TEMPLATE_FIELDS,
        ),
        "candidate_tag_template": (
            candidate_template,
            CANDIDATE_TEMPLATE_FIELDS,
        ),
        "candidate_arch_tag_template": (
            candidate_arch_template,
            CANDIDATE_ARCH_TEMPLATE_FIELDS,
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

    for stream in streams:
        if not isinstance(stream, dict) or stream.get("id") not in EXPECTED_STREAMS:
            continue
        stream_id = stream["id"]
        try:
            deploy_tag = deploy_template.format(**stream)
            if deploy_tag != stream_id:
                errors.append(
                    f"deploy tag for stream {stream_id!r} must equal the stream ID"
                )
            candidate_tag = candidate_template.format(
                **stream,
                candidate_id="123456789-1",
            )
            if candidate_tag != f"{stream_id}-candidate-123456789-1":
                errors.append(f"candidate tag for stream {stream_id!r} is invalid")
            for arch in EXPECTED_ARCHITECTURES:
                candidate_arch_tag = candidate_arch_template.format(
                    **stream,
                    candidate_id="123456789-1",
                    arch=arch,
                )
                expected = f"{stream_id}-candidate-123456789-1-{arch}"
                if candidate_arch_tag != expected:
                    errors.append(
                        f"candidate architecture tag for {stream_id!r}/{arch!r} "
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

    streams = matrix.get("streams")
    stream_objects = (
        [stream for stream in streams if isinstance(stream, dict)]
        if isinstance(streams, list)
        else []
    )
    accepted = {
        field: {
            stream.get(stream_field)
            for stream in stream_objects
            if isinstance(stream.get(stream_field), str)
        }
        for field, stream_field in SELECTOR_FIELDS.items()
    }

    selector_is_valid = not unknown
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
            elif value not in accepted[field]:
                errors.append(
                    f"{field_context} contains unsupported value: {value!r}"
                )
                selector_is_valid = False

    if selector_is_valid and not any(
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
    parents: list[str] = []
    for group in build_groups:
        group_images = group.get("images")
        if not isinstance(group_images, list):
            continue
        if selected_images is not None and selected_images.isdisjoint(group_images):
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
            if isinstance(parent, str) and parent not in parents:
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
    actual_parents = resolved_parent_sequence(build_groups)
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
        if set(reviewed_streams) != set(EXPECTED_STREAMS):
            errors.append(
                f"{context} reviewed_streams must be exactly "
                f"{sorted(EXPECTED_STREAMS)!r}"
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
        profile = load_json(profile_path)
        if not isinstance(profile, dict):
            errors.append(f"{profile_path.relative_to(ROOT)} must contain an object")
            continue
        validate_profile(matrix, profile_name, profile, errors)


def main() -> int:
    errors: list[str] = []
    matrix = load_json(MATRIX_PATH)
    if not isinstance(matrix, dict):
        errors.append(f"{MATRIX_PATH.relative_to(ROOT)} must contain an object")
    else:
        validate_matrix(matrix, errors)
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
