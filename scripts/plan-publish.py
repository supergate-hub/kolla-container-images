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
ARCH_TO_RUNNER_LABELS = {
    "amd64": ["self-hosted", "linux", "x64", "kolla-build"],
    "arm64": ["self-hosted", "linux", "ARM64", "kolla-build"],
}
KOLLA_BUILD_THREADS = 4
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
    images: list[str],
    arch: str,
    arch_tag: str,
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
        f"artifacts/kolla-summary/{stream['id']}-{arch}.json",
        "--logs-dir",
        f"artifacts/kolla-logs/{stream['id']}-{arch}",
        "--push",
        *[f"^{image}$" for image in images],
    ]


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
    selected_groups = selected_build_groups(profile, selected_images)
    selected_names = [entry["name"] for entry in selected_images]
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

    parent_images = list(
        dict.fromkeys(
            parent for group in selected_groups for parent in group["parents"]
        )
    )
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
                "runner_labels": ARCH_TO_RUNNER_LABELS[arch],
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
                "commands": {
                    "kolla_build_push": kolla_build_command(
                        matrix, stream, selected_names, arch, arch_tag
                    )
                },
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
        "build": {"architectures": build_architectures},
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
