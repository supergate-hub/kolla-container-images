#!/usr/bin/env python3
"""Create a dry-run publish plan for Kolla image artifacts."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from publish_approval import approval_requirement
from profile_resolver import (
    LOCAL_DRY_RUN_CANDIDATE_ID,
    find_stream,
    load_matrix,
    load_profile,
    render_candidate_tag,
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
    return parser.parse_args()


def image_ref(registry: str, owner: str, repository: str, image: str, tag: str) -> str:
    return f"{registry}/{owner}/{repository}/{image}:{tag}"


def manifest_metadata_file(image: str, deploy_tag: str) -> str:
    return f"artifacts/manifests/{image}-{deploy_tag}.json"


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
) -> list[str]:
    return [
        "kolla-build",
        "--engine",
        "docker",
        "--base",
        stream["distro"],
        "--base-tag",
        stream["base_tag"],
        "--base-arch",
        ARCH_TO_KOLLA_BASE_ARCH[arch],
        "--platform",
        ARCH_TO_PLATFORM[arch],
        "--openstack-release",
        stream["release"],
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
        "--skip-existing",
        "--push",
        f"^{target}$",
    ]


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
) -> dict[str, Any]:
    arch_tag = render_candidate_tag(matrix, stream, candidate_id, arch)
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
        ),
    }


def build_plan(
    matrix: dict[str, Any],
    profile: dict[str, Any],
    stream: dict[str, Any],
    image_filter: str | None = None,
    candidate_id: str = LOCAL_DRY_RUN_CANDIDATE_ID,
) -> dict[str, Any]:
    candidate_id = validate_candidate_id(candidate_id)
    stream_tag = render_tag(matrix, stream)
    candidate_tag = render_candidate_tag(matrix, stream, candidate_id)
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
    requirement = approval_requirement(
        f"{registry}/{owner}/{repository}",
        stream["id"],
        profile["name"],
        scope_image,
        len(selected_images),
    )

    images = []
    for image_entry in selected_images:
        image = image_entry["name"]
        architectures = []
        for arch in matrix["architectures"]:
            arch_tag = render_candidate_tag(matrix, stream, candidate_id, arch)
            arch_ref = image_ref(registry, owner, repository, image, arch_tag)
            architectures.append(
                {
                    "arch": arch,
                    "arch_tag": arch_tag,
                    "arch_ref": arch_ref,
                    "expected_ghcr_ref": arch_ref,
                    "kolla_base_arch": ARCH_TO_KOLLA_BASE_ARCH[arch],
                    "platform": ARCH_TO_PLATFORM[arch],
                }
            )

        deploy_ref = image_ref(
            registry, owner, repository, image, candidate_tag
        )
        stream_ref = image_ref(
            registry, owner, repository, image, stream_tag
        )
        arch_refs = [architecture["arch_ref"] for architecture in architectures]
        images.append(
            {
                "image": image,
                "kolla_ansible_variables": image_entry["kolla_ansible_variables"],
                "deploy_tag": candidate_tag,
                "deploy_ref": deploy_ref,
                "stream_ref": stream_ref,
                "expected_ghcr_ref": deploy_ref,
                "manifest_metadata_file": manifest_metadata_file(image, candidate_tag),
                "architectures": architectures,
                "commands": {
                    "manifest_create": [
                        "docker",
                        "buildx",
                        "imagetools",
                        "create",
                        "--tag",
                        deploy_ref,
                        "--metadata-file",
                        manifest_metadata_file(image, candidate_tag),
                        *arch_refs,
                    ],
                    "manifest_inspect": [
                        "docker",
                        "buildx",
                        "imagetools",
                        "inspect",
                        deploy_ref,
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

    parent_images = list(parent_chains)
    images_by_name = {image["image"]: image for image in images}
    build_architectures = []
    for arch in matrix["architectures"]:
        arch_tag = render_candidate_tag(matrix, stream, candidate_id, arch)
        platform = ARCH_TO_PLATFORM[arch]
        build_architectures.append(
            {
                "arch": arch,
                "arch_tag": arch_tag,
                "kolla_base_arch": ARCH_TO_KOLLA_BASE_ARCH[arch],
                "platform": platform,
                "runner_labels": [ARCH_TO_RUNNER[arch]],
                "parents": [
                    {
                        "image": parent,
                        "arch_ref": image_ref(
                            registry, owner, repository, parent, arch_tag
                        ),
                    }
                    for parent in parent_images
                ],
                "images": [
                    {
                        "image": image,
                        "arch_ref": next(
                            architecture["arch_ref"]
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
        "dry_run": True,
        "candidate_id": candidate_id,
        "stream": stream["id"],
        "release": stream["release"],
        "distro": stream["distro"],
        "distro_version": stream["base_tag"],
        "kolla_version": stream["kolla_version"],
        "kolla_ansible_version": stream["kolla_ansible_version"],
        "profile": profile["name"],
        "image_filter": image_filter,
        "scope": {
            "profile": profile["name"],
            "image": scope_image,
            "image_count": len(selected_images),
        },
        "approval": {
            "allowed": requirement is not None,
            "required_variable": requirement.variable if requirement else None,
            "phrase": requirement.phrase if requirement else None,
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
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    print(json.dumps(plan, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
