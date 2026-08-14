#!/usr/bin/env python3
"""Create a dry-run publish plan for Kolla image artifacts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from base_resolution import resolve_base, validate_resolved_base
from openstack_source_set import render_frozen_configs
from profile_resolver import (
    LOCAL_DRY_RUN_CANDIDATE_ID,
    find_stream,
    load_matrix,
    load_profile,
    render_revision_tag,
    render_tag,
    resolve_profile,
    validate_candidate_id,
)


ARCH_TO_KOLLA_BASE_ARCH = {
    "amd64": "x86_64",
    "arm64": "aarch64",
}
ARCH_TO_PLATFORM = {
    "amd64": "linux/amd64",
    "arm64": "linux/arm64",
}
ARCH_TO_RUNNER = {
    "amd64": "ubuntu-24.04",
    "arm64": "ubuntu-24.04-arm",
}
ARCH_TO_RUNNER_MACHINE = {
    "amd64": "x86_64",
    "arm64": "aarch64",
}
PARENT_TIERS = (0, 1, 2)
LEAF_STAGES = (0, 1)
LEAF_TIER = 3
KOLLA_BUILD_THREADS = 1
KOLLA_PUSH_THREADS = 1
KOLLA_BUILD_CONFIG_FILE = "artifacts/config/kolla-build.conf"
KOLLA_TEMPLATE_OVERRIDE_FILE = "artifacts/config/template-overrides.j2"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render a dry-run Kolla image publish plan from repository config."
    )
    parser.add_argument("--stream", required=True, help="Build stream ID")
    parser.add_argument("--profile", required=True, help="Profile name under config/profiles")
    parser.add_argument("--image", help="Optional image name from the selected profile")
    parser.add_argument(
        "--candidate-id",
        default=LOCAL_DRY_RUN_CANDIDATE_ID,
        help=(
            "Workflow run candidate ID; local read-only plans default to "
            f"{LOCAL_DRY_RUN_CANDIDATE_ID}"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        required=True,
        help="Required safety flag. This planner never builds or pushes images.",
    )
    base_group = parser.add_mutually_exclusive_group()
    base_group.add_argument(
        "--base-manifest",
        type=Path,
        help="Use checked raw OCI index bytes instead of resolving the mutable base tag.",
    )
    base_group.add_argument(
        "--frozen-base-resolution",
        type=Path,
        help="Revalidate a prior frozen base result without resolving its mutable tag.",
    )
    return parser.parse_args()


def image_ref(registry: str, owner: str, repository: str, image: str, tag: str) -> str:
    return f"{registry}/{owner}/{repository}/{image}:{tag}"


def manifest_metadata_file(image: str, revision_tag: str) -> str:
    return f"artifacts/manifests/{image}-{revision_tag}.json"


def publish_summary_file(stream_id: str) -> str:
    return f"artifacts/publish-summary-{stream_id}.json"


def kolla_ansible_lock_file(stream_id: str) -> str:
    return f"artifacts/kolla-ansible-image-lock-{stream_id}.yml"


def profile_images(profile: dict[str, Any], image_filter: str | None) -> list[dict[str, Any]]:
    images = profile["images"]
    if image_filter is None:
        return images
    image_names = {entry["name"] for entry in images}
    if image_filter not in image_names:
        raise ValueError(f"image does not exist in profile {profile['name']}: {image_filter}")
    return [entry for entry in images if entry["name"] == image_filter]


def selected_build_groups(
    profile: dict[str, Any], selected_images: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    selected_names = {entry["name"] for entry in selected_images}
    groups = []
    for group in profile["build_groups"]:
        group_images = [image for image in group["images"] if image in selected_names]
        if not group_images:
            continue
        parents = group.get("parents")
        if parents is None:
            parents = list(dict.fromkeys(["base", "openstack-base", group["parent"]]))
        groups.append({**group, "parents": parents, "images": group_images})
    return groups


def kolla_build_command(
    matrix: dict[str, Any],
    stream: dict[str, Any],
    target: str,
    arch: str,
    arch_tag: str,
    summary_file: str,
    logs_dir: str,
    use_template_override: bool,
) -> list[str]:
    command = [
        "kolla-build",
        "--engine",
        "docker",
        "--base",
        stream["distro"],
        "--base-image",
        stream["base_image"],
        "--base-tag",
        stream["base_tag"],
        "--base-arch",
        ARCH_TO_KOLLA_BASE_ARCH[arch],
        "--platform",
        ARCH_TO_PLATFORM[arch],
        "--openstack-release",
        stream["release"],
        "--config-file",
        KOLLA_BUILD_CONFIG_FILE,
        "--locals-base",
        ".",
        "--registry",
        matrix["registry"],
        "--namespace",
        f"{matrix['owner']}/{matrix['repository']}",
        "--tag",
        arch_tag,
        "--threads",
        str(KOLLA_BUILD_THREADS),
        "--push-threads",
        str(KOLLA_PUSH_THREADS),
        "--summary-json-file",
        summary_file,
        "--logs-dir",
        logs_dir,
        "--nopull",
        "--skip-existing",
        "--push",
    ]
    if use_template_override:
        command.extend(["--template-override", KOLLA_TEMPLATE_OVERRIDE_FILE])
    command.append(f"^{target}$")
    return command


def selected_parent_chains(
    groups: list[dict[str, Any]],
    leaf_chains: dict[str, list[str]],
) -> dict[str, list[str]]:
    """Return non-leaf parents and the ancestors that must already exist."""
    chains: dict[str, list[str]] = {}
    for group in groups:
        parents = group["parents"]
        for index, parent in enumerate(parents):
            ancestor_chain = parents[:index]
            existing = chains.get(parent)
            if existing is not None and existing != ancestor_chain:
                raise ValueError(
                    f"inconsistent ancestor chain for parent {parent}: "
                    f"{existing!r} != {ancestor_chain!r}"
                )
            chains.setdefault(parent, ancestor_chain)

    for leaf, leaf_chain in leaf_chains.items():
        parent_chain = chains.get(leaf)
        if parent_chain is not None and parent_chain != leaf_chain:
            raise ValueError(
                f"selected leaf {leaf} has inconsistent dependency chains: "
                f"{leaf_chain!r} != {parent_chain!r}"
            )

    return {
        parent: ancestor_chain
        for parent, ancestor_chain in chains.items()
        if parent not in leaf_chains
    }


def selected_leaf_chains(
    groups: list[dict[str, Any]],
) -> dict[str, list[str]]:
    """Return the exact root-to-leaf parent chain for each selected leaf."""
    chains: dict[str, list[str]] = {}
    for group in groups:
        for image in group["images"]:
            existing = chains.get(image)
            if existing is not None:
                raise ValueError(f"image belongs to multiple build groups: {image}")
            chains[image] = group["parents"]
    return chains


def selected_leaf_dependency_closure(
    selected_names: list[str],
    catalog_names: list[str],
    catalog_leaf_chains: dict[str, list[str]],
) -> list[str]:
    """Include catalog leaves needed to build the requested publish leaves."""
    missing = sorted(set(selected_names) - set(catalog_leaf_chains))
    if missing:
        raise ValueError(f"selected images are missing build groups: {missing}")

    required = set(selected_names)
    while True:
        dependencies = {
            ancestor
            for image in required
            for ancestor in catalog_leaf_chains[image]
            if ancestor in catalog_leaf_chains
        }
        expanded = required | dependencies
        if expanded == required:
            break
        required = expanded

    return [name for name in catalog_names if name in required]


def selected_leaf_stage_map(
    leaf_chains: dict[str, list[str]],
) -> dict[str, int]:
    """Topologically assign selected leaves to the two supported build stages."""
    stages: dict[str, int] = {}
    visiting: list[str] = []

    def stage_for(image: str) -> int:
        if image in stages:
            return stages[image]
        if image in visiting:
            cycle_start = visiting.index(image)
            cycle = [*visiting[cycle_start:], image]
            raise ValueError(
                "selected leaf dependency cycle: " + " -> ".join(cycle)
            )

        visiting.append(image)
        selected_dependencies = [
            ancestor
            for ancestor in leaf_chains[image]
            if ancestor in leaf_chains
        ]
        stage = max(
            (stage_for(dependency) + 1 for dependency in selected_dependencies),
            default=0,
        )
        visiting.pop()
        if stage not in LEAF_STAGES:
            raise ValueError(
                f"selected leaf dependency depth exceeds supported stages for "
                f"{image}: stage {stage}; supported stages: {list(LEAF_STAGES)}"
            )
        stages[image] = stage
        return stage

    for image in leaf_chains:
        stage_for(image)
    return stages


def build_unit(
    matrix: dict[str, Any],
    stream: dict[str, Any],
    candidate_id: str,
    *,
    kind: str,
    tier: int,
    arch: str,
    target: str,
    ancestor_chain: list[str],
    use_template_override: bool,
) -> dict[str, Any]:
    arch_tag = render_revision_tag(matrix, stream, candidate_id, arch)
    unit_id = f"{arch}-{kind}-{target}"
    summary_file = (
        f"artifacts/kolla-summary/{stream['id']}/{candidate_id}/{unit_id}.json"
    )
    logs_dir = f"artifacts/kolla-logs/{stream['id']}/{candidate_id}/{unit_id}"
    registry = matrix["registry"]
    owner = matrix["owner"]
    repository = matrix["repository"]
    return {
        "id": unit_id,
        "kind": kind,
        "tier": tier,
        "arch": arch,
        "runner": ARCH_TO_RUNNER[arch],
        "runner_machine": ARCH_TO_RUNNER_MACHINE[arch],
        "kolla_base_arch": ARCH_TO_KOLLA_BASE_ARCH[arch],
        "platform": ARCH_TO_PLATFORM[arch],
        "target": target,
        "ancestor_chain": ancestor_chain,
        "ancestors": [
            {
                "image": ancestor,
                "arch_ref": image_ref(
                    registry, owner, repository, ancestor, arch_tag
                ),
            }
            for ancestor in ancestor_chain
        ],
        "arch_ref": image_ref(registry, owner, repository, target, arch_tag),
        "summary_file": summary_file,
        "logs_dir": logs_dir,
        "command": kolla_build_command(
            matrix,
            stream,
            target,
            arch,
            arch_tag,
            summary_file,
            logs_dir,
            use_template_override,
        ),
    }


def build_plan(
    matrix: dict[str, Any],
    profile: dict[str, Any],
    stream: dict[str, Any],
    image_filter: str | None = None,
    candidate_id: str = LOCAL_DRY_RUN_CANDIDATE_ID,
    base_manifest: bytes | None = None,
    frozen_base_resolution: dict[str, Any] | None = None,
) -> dict[str, Any]:
    candidate_id = validate_candidate_id(candidate_id)
    semantic_tag = render_tag(matrix, stream)
    revision_manifest_tag = render_revision_tag(matrix, stream, candidate_id)
    registry = matrix["registry"]
    owner = matrix["owner"]
    repository = matrix["repository"]
    selected_images = profile_images(profile, image_filter)
    selected_names = [entry["name"] for entry in selected_images]
    catalog_names = [entry["name"] for entry in profile["images"]]
    catalog_groups = selected_build_groups(profile, profile["images"])
    catalog_leaf_chains = selected_leaf_chains(catalog_groups)
    build_leaf_names = selected_leaf_dependency_closure(
        selected_names,
        catalog_names,
        catalog_leaf_chains,
    )
    build_leaf_name_set = set(build_leaf_names)
    build_leaf_entries = [
        entry for entry in profile["images"] if entry["name"] in build_leaf_name_set
    ]
    selected_groups = selected_build_groups(profile, build_leaf_entries)
    scope_image = image_filter or "all"
    frozen_sources = render_frozen_configs(stream["source_set"])
    use_template_override = bool(frozen_sources.template_override_content)

    images = []
    for image_entry in selected_images:
        image = image_entry["name"]
        architectures = []
        for arch in matrix["architectures"]:
            arch_tag = render_revision_tag(matrix, stream, candidate_id, arch)
            arch_ref = image_ref(registry, owner, repository, image, arch_tag)
            architectures.append(
                {
                    "arch": arch,
                    "revision_arch_tag": arch_tag,
                    "revision_arch_ref": arch_ref,
                    "kolla_base_arch": ARCH_TO_KOLLA_BASE_ARCH[arch],
                    "platform": ARCH_TO_PLATFORM[arch],
                }
            )

        semantic_ref = image_ref(
            registry, owner, repository, image, semantic_tag
        )
        revision_ref = image_ref(
            registry, owner, repository, image, revision_manifest_tag
        )
        arch_refs = [
            architecture["revision_arch_ref"] for architecture in architectures
        ]
        images.append(
            {
                "image": image,
                "kolla_ansible_variables": image_entry["kolla_ansible_variables"],
                "semantic_tag": semantic_tag,
                "semantic_ref": semantic_ref,
                "revision_tag": revision_manifest_tag,
                "revision_ref": revision_ref,
                "manifest_metadata_file": manifest_metadata_file(
                    image, revision_manifest_tag
                ),
                "architectures": architectures,
                "commands": {
                    "manifest_create": [
                        "docker",
                        "buildx",
                        "imagetools",
                        "create",
                        "--tag",
                        revision_ref,
                        "--metadata-file",
                        manifest_metadata_file(image, revision_manifest_tag),
                        *arch_refs,
                    ],
                    "manifest_inspect": [
                        "docker",
                        "buildx",
                        "imagetools",
                        "inspect",
                        revision_ref,
                    ],
                },
            }
        )

    leaf_chains = selected_leaf_chains(selected_groups)
    if set(leaf_chains) != set(build_leaf_names):
        missing = sorted(set(build_leaf_names) - set(leaf_chains))
        raise ValueError(f"build images are missing build groups: {missing}")
    parent_chains = selected_parent_chains(selected_groups, leaf_chains)
    leaf_stage_by_image = selected_leaf_stage_map(leaf_chains)

    parent_units_by_tier: dict[int, list[dict[str, Any]]] = {
        tier: [] for tier in PARENT_TIERS
    }
    leaf_units_by_stage: dict[int, list[dict[str, Any]]] = {
        stage: [] for stage in LEAF_STAGES
    }
    for arch in matrix["architectures"]:
        for parent, ancestor_chain in parent_chains.items():
            tier = len(ancestor_chain)
            if tier not in parent_units_by_tier:
                raise ValueError(
                    f"unsupported parent tier {tier} for {parent}; "
                    f"supported tiers: {list(PARENT_TIERS)}"
                )
            parent_units_by_tier[tier].append(
                build_unit(
                    matrix,
                    stream,
                    candidate_id,
                    kind="parent",
                    tier=tier,
                    arch=arch,
                    target=parent,
                    ancestor_chain=ancestor_chain,
                    use_template_override=use_template_override,
                )
            )
    for stage in LEAF_STAGES:
        for arch in matrix["architectures"]:
            for image in build_leaf_names:
                if leaf_stage_by_image[image] != stage:
                    continue
                leaf_units_by_stage[stage].append(
                    build_unit(
                        matrix,
                        stream,
                        candidate_id,
                        kind="leaf",
                        tier=LEAF_TIER + stage,
                        arch=arch,
                        target=image,
                        ancestor_chain=leaf_chains[image],
                        use_template_override=use_template_override,
                    )
                )

    parent_tiers = [
        {
            "tier": tier,
            "matrix": {"include": parent_units_by_tier[tier]},
        }
        for tier in PARENT_TIERS
    ]
    leaf_stages = [
        {
            "stage": stage,
            "matrix": {"include": leaf_units_by_stage[stage]},
        }
        for stage in LEAF_STAGES
    ]
    all_units = [
        unit
        for tier in PARENT_TIERS
        for unit in parent_units_by_tier[tier]
    ] + [
        unit
        for stage in LEAF_STAGES
        for unit in leaf_units_by_stage[stage]
    ]
    unit_ids = [unit["id"] for unit in all_units]
    if len(unit_ids) != len(set(unit_ids)):
        raise ValueError("build unit IDs must be unique")

    configured_base = {
        "id": stream["base_id"],
        "distro": stream["distro"],
        "os_version": stream["os_version"],
        "image": stream["base_image"],
        "tag": stream["base_tag"],
    }
    resolved_base = (
        validate_resolved_base(configured_base, frozen_base_resolution)
        if frozen_base_resolution is not None
        else resolve_base(configured_base, base_manifest)
    )

    parent_images = list(parent_chains)
    images_by_name = {image["image"]: image for image in images}
    build_architectures = []
    for arch in matrix["architectures"]:
        arch_tag = render_revision_tag(matrix, stream, candidate_id, arch)
        platform = ARCH_TO_PLATFORM[arch]
        build_architectures.append(
            {
                "arch": arch,
                "revision_arch_tag": arch_tag,
                "kolla_base_arch": ARCH_TO_KOLLA_BASE_ARCH[arch],
                "platform": platform,
                "runner_labels": [ARCH_TO_RUNNER[arch]],
                "parents": [
                    {
                        "image": parent,
                        "revision_arch_ref": image_ref(
                            registry, owner, repository, parent, arch_tag
                        ),
                    }
                    for parent in parent_images
                ],
                "images": [
                    {
                        "image": image,
                        "revision_arch_ref": next(
                            architecture["revision_arch_ref"]
                            for architecture in images_by_name[image]["architectures"]
                            if architecture["arch"] == arch
                        ),
                        "smoke": {
                            "ref_source": "recorded_child_digest",
                            "platform": platform,
                            "inspect_platform": True,
                            "entrypoint": "/bin/true",
                        },
                    }
                    for image in selected_names
                ],
            }
        )

    return {
        "schema_version": 3,
        "candidate_id": candidate_id,
        "stream": stream["id"],
        "release": stream["release"],
        "release_series": stream["release_series"],
        "release_branch": stream["release_branch"],
        "distro": stream["distro"],
        "distro_version": stream["base_tag"],
        "base": resolved_base,
        "openstack_sources": {
            "source_set": stream["source_set"],
            "canonical_digest": stream["source_set_sha256"],
            "kolla_build_config": {
                "sha256": frozen_sources.config_sha256,
                "content": frozen_sources.config_content,
            },
            "template_override": {
                "sha256": frozen_sources.template_override_sha256,
                "content": frozen_sources.template_override_content,
            },
        },
        "release_metadata": {
            "repository": matrix["release_metadata"]["repository"],
            "commit": matrix["release_metadata"]["commit"],
        },
        "kolla": {
            "repository": stream["kolla_repository"],
            "version": stream["kolla_version"],
            "commit": stream["kolla_commit"],
        },
        "kolla_ansible": {
            "repository": stream["kolla_ansible_repository"],
            "version": stream["kolla_ansible_version"],
            "commit": stream["kolla_ansible_commit"],
        },
        "profile": profile["name"],
        "image_filter": image_filter,
        "scope": {
            "profile": profile["name"],
            "image": scope_image,
            "image_count": len(selected_images),
        },
        "registry": registry,
        "owner": owner,
        "repository": repository,
        "publish_summary_file": publish_summary_file(stream["id"]),
        "kolla_ansible_lock_file": (
            kolla_ansible_lock_file(stream["id"])
            if profile["name"] == "deployment" and image_filter is None
            else None
        ),
        "build": {
            "architectures": build_architectures,
            "parent_tiers": parent_tiers,
            "leaf_stages": leaf_stages,
            "all_units": all_units,
        },
        "images": images,
    }


def main() -> int:
    args = parse_args()
    if not args.dry_run:
        print("Refusing to render publish plan without --dry-run.", file=sys.stderr)
        return 2

    try:
        matrix = load_matrix()
        stream = find_stream(matrix, args.stream)
        profile = resolve_profile(load_profile(args.profile), stream)
        plan = build_plan(
            matrix,
            profile,
            stream,
            args.image,
            args.candidate_id,
            args.base_manifest.read_bytes() if args.base_manifest else None,
            (
                json.loads(args.frozen_base_resolution.read_text(encoding="utf-8"))
                if args.frozen_base_resolution
                else None
            ),
        )
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    print(json.dumps(plan, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
