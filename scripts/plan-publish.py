#!/usr/bin/env python3
"""Create a dry-run publish plan for Kolla image artifacts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
MATRIX_PATH = ROOT / "config" / "build-matrix.json"
PROFILES_DIR = ROOT / "config" / "profiles"
ARCH_TO_KOLLA_BASE_ARCH = {
    "amd64": "x86_64",
    "arm64": "aarch64",
}
ARCH_TO_PLATFORM = {
    "amd64": "linux/amd64",
    "arm64": "linux/arm64",
}
KOLLA_BUILD_THREADS = 4
KOLLA_PUSH_THREADS = 1


def load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as file_obj:
        return json.load(file_obj)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render a dry-run Kolla image publish plan from repository config."
    )
    parser.add_argument("--profile", required=True, help="Profile name under config/profiles")
    parser.add_argument("--release", required=True, help="OpenStack release, for example 2025.1")
    parser.add_argument("--distro", required=True, help="Base distro name, for example rocky")
    parser.add_argument("--distro-version", required=True, help="Base distro version, for example 9")
    parser.add_argument("--image", help="Optional image name from the selected profile")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Required safety flag. This planner never builds or pushes images.",
    )
    return parser.parse_args()


def find_distro(matrix: dict[str, Any], name: str, version: str) -> dict[str, str]:
    for distro in matrix["distros"]:
        if distro["name"] == name and distro["version"] == version:
            return distro
    raise ValueError(f"unsupported distro/version: {name}-{version}")


def load_profile(name: str) -> dict[str, Any]:
    profile_path = PROFILES_DIR / f"{name}.json"
    if not profile_path.exists():
        raise ValueError(f"profile does not exist: {profile_path.relative_to(ROOT)}")
    profile = load_json(profile_path)
    if profile.get("name") != name:
        raise ValueError(f"profile name mismatch in {profile_path.relative_to(ROOT)}")
    return profile


def render_tag(template: str, release: str, distro: dict[str, str], arch: str | None = None) -> str:
    values = {
        "release": release,
        "distro": distro["name"],
        "distro_version": distro["version"],
        "arch": arch or "",
    }
    return template.format(**values)


def image_ref(registry: str, owner: str, repository: str, image: str, tag: str) -> str:
    return f"{registry}/{owner}/{repository}/{image}:{tag}"


def manifest_metadata_file(image: str, deploy_tag: str) -> str:
    return f"artifacts/manifests/{image}-{deploy_tag}.json"


def publish_summary_file(deploy_tag: str) -> str:
    return f"artifacts/publish-summary-{deploy_tag}.json"


def kolla_ansible_lock_file(deploy_tag: str) -> str:
    return f"artifacts/kolla-ansible-image-lock-{deploy_tag}.yml"


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
        if group_images:
            parents = group.get("parents")
            if parents is None:
                parents = list(
                    dict.fromkeys(["base", "openstack-base", group["parent"]])
                )
            groups.append(
                {
                    "name": group["name"],
                    "parent": group["parent"],
                    "parents": parents,
                    "images": group_images,
                }
            )
    return groups


def kolla_build_command(
    registry: str,
    owner: str,
    repository: str,
    images: list[str],
    release: str,
    distro: dict[str, str],
    arch: str,
    arch_tag: str,
    summary_file: str,
    logs_dir: str,
    skip_existing: bool = False,
) -> list[str]:
    command = [
        "kolla-build",
        "--engine",
        "docker",
        "--base",
        distro["name"],
        "--base-tag",
        distro["version"],
        "--base-arch",
        ARCH_TO_KOLLA_BASE_ARCH[arch],
        "--platform",
        ARCH_TO_PLATFORM[arch],
        "--openstack-release",
        release,
        "--registry",
        registry,
        "--namespace",
        f"{owner}/{repository}",
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
    ]
    if skip_existing:
        command.append("--skip-existing")
    command.extend(
        [
            "--push",
            *[f"^{image}$" for image in images],
        ]
    )
    return command


def build_plan(
    matrix: dict[str, Any],
    profile: dict[str, Any],
    release: str,
    distro: dict[str, str],
    image_filter: str | None = None,
) -> dict[str, Any]:
    tag_policy = matrix["tag_policy"]
    registry = matrix["registry"]
    owner = matrix["owner"]
    repository = matrix["repository"]
    deploy_tag = render_tag(tag_policy["deploy_tag_template"], release, distro)
    selected_images = profile_images(profile, image_filter)
    build_groups = selected_build_groups(profile, selected_images)

    images = []
    for image_entry in selected_images:
        image = image_entry["name"]
        architectures = []
        for arch in matrix["architectures"]:
            arch_tag = render_tag(tag_policy["arch_tag_template"], release, distro, arch)
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

        deploy_ref = image_ref(registry, owner, repository, image, deploy_tag)
        arch_refs = [arch_plan["arch_ref"] for arch_plan in architectures]
        metadata_file = manifest_metadata_file(image, deploy_tag)
        images.append(
            {
                "image": image,
                "kolla_ansible_variables": image_entry["kolla_ansible_variables"],
                "deploy_tag": deploy_tag,
                "deploy_ref": deploy_ref,
                "expected_ghcr_ref": deploy_ref,
                "manifest_metadata_file": metadata_file,
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
                        metadata_file,
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
            [
                "base",
                "openstack-base",
                *[
                    parent
                    for group in build_groups
                    for parent in group["parents"]
                ],
            ]
        )
    )
    parent_architectures = []
    for arch in matrix["architectures"]:
        arch_tag = render_tag(tag_policy["arch_tag_template"], release, distro, arch)
        parent_architectures.append(
            {
                "arch": arch,
                "arch_tag": arch_tag,
                "platform": ARCH_TO_PLATFORM[arch],
                "parent_refs": [
                    image_ref(registry, owner, repository, parent, arch_tag)
                    for parent in parent_images
                ],
                "commands": {
                    "kolla_build_push": kolla_build_command(
                        registry,
                        owner,
                        repository,
                        parent_images,
                        release,
                        distro,
                        arch,
                        arch_tag,
                        f"artifacts/kolla-summary/parents-{arch}.json",
                        f"artifacts/kolla-logs/parents-{arch}",
                    )
                },
            }
        )

    planned_build_groups = []
    images_by_name = {image["image"]: image for image in images}
    for group in build_groups:
        group_architectures = []
        for arch in matrix["architectures"]:
            arch_tag = render_tag(tag_policy["arch_tag_template"], release, distro, arch)
            group_architectures.append(
                {
                    "arch": arch,
                    "arch_tag": arch_tag,
                    "platform": ARCH_TO_PLATFORM[arch],
                    "parent_refs": [
                        image_ref(registry, owner, repository, parent, arch_tag)
                        for parent in group["parents"]
                    ],
                    "image_refs": [
                        next(
                            arch_plan["arch_ref"]
                            for arch_plan in images_by_name[image]["architectures"]
                            if arch_plan["arch"] == arch
                        )
                        for image in group["images"]
                    ],
                    "commands": {
                        "kolla_build_push": kolla_build_command(
                            registry,
                            owner,
                            repository,
                            group["images"],
                            release,
                            distro,
                            arch,
                            arch_tag,
                            f"artifacts/kolla-summary/{group['name']}-{arch}.json",
                            f"artifacts/kolla-logs/{group['name']}-{arch}",
                            skip_existing=True,
                        )
                    },
                }
            )
        planned_build_groups.append({**group, "architectures": group_architectures})

    return {
        "dry_run": True,
        "release": release,
        "distro": distro["name"],
        "distro_version": distro["version"],
        "profile": profile["name"],
        "image_filter": image_filter,
        "registry": registry,
        "owner": owner,
        "repository": repository,
        "publish_summary_file": publish_summary_file(deploy_tag),
        "kolla_ansible_lock_file": kolla_ansible_lock_file(deploy_tag),
        "build": {
            "parents": {
                "images": parent_images,
                "architectures": parent_architectures,
            },
            "groups": planned_build_groups,
        },
        "images": images,
    }


def main() -> int:
    args = parse_args()
    if not args.dry_run:
        print("Refusing to render publish plan without --dry-run.", file=sys.stderr)
        return 2

    matrix = load_json(MATRIX_PATH)
    if args.release != matrix["release"]:
        print(f"Unsupported release: {args.release}", file=sys.stderr)
        return 2

    try:
        distro = find_distro(matrix, args.distro, args.distro_version)
        profile = load_profile(args.profile)
        plan = build_plan(matrix, profile, args.release, distro, args.image)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    print(json.dumps(plan, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
