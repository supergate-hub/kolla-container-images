#!/usr/bin/env python3
"""Validate kolla-container-images repository configuration."""

from __future__ import annotations

import json
import re
import string
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
MATRIX_PATH = ROOT / "config" / "build-matrix.json"
PROFILES_DIR = ROOT / "config" / "profiles"

EXPECTED_RELEASE = "2025.1"
ALLOWED_DISTROS = {"rocky", "ubuntu"}
ALLOWED_ARCHITECTURES = {"amd64", "arm64"}
REQUIRED_TEMPLATE_FIELDS = {"release", "distro", "distro_version"}
ARCH_TEMPLATE_FIELDS = REQUIRED_TEMPLATE_FIELDS | {"arch"}
IMAGE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
BUILD_GROUP_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
KOLLA_IMAGE_VARIABLE_RE = re.compile(r"^[a-z0-9_]+_image_full$")


def load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as file_obj:
        return json.load(file_obj)


def template_fields(template: str) -> set[str]:
    return {
        field_name
        for _, field_name, _, _ in string.Formatter().parse(template)
        if field_name
    }


def render_tag(template: str, release: str, distro: dict[str, str], arch: str) -> str:
    return template.format(
        release=release,
        distro=distro["name"],
        distro_version=distro["version"],
        arch=arch,
    )


def validate_matrix(matrix: dict[str, Any], errors: list[str]) -> None:
    if matrix.get("release") != EXPECTED_RELEASE:
        errors.append(f"release must be {EXPECTED_RELEASE!r}")

    distros = matrix.get("distros")
    if not isinstance(distros, list) or not distros:
        errors.append("distros must be a non-empty list")
        distros = []

    for index, distro in enumerate(distros):
        if not isinstance(distro, dict):
            errors.append(f"distros[{index}] must be an object")
            continue
        name = distro.get("name")
        version = distro.get("version")
        if name not in ALLOWED_DISTROS:
            errors.append(f"distros[{index}].name must be one of {sorted(ALLOWED_DISTROS)}")
        if not isinstance(version, str) or not version:
            errors.append(f"distros[{index}].version must be a non-empty string")

    architectures = matrix.get("architectures")
    if set(architectures or []) != ALLOWED_ARCHITECTURES:
        errors.append(f"architectures must be exactly {sorted(ALLOWED_ARCHITECTURES)}")

    tag_policy = matrix.get("tag_policy")
    if not isinstance(tag_policy, dict):
        errors.append("tag_policy must be an object")
        return

    deploy_template = tag_policy.get("deploy_tag_template")
    arch_template = tag_policy.get("arch_tag_template")
    if not isinstance(deploy_template, str):
        errors.append("tag_policy.deploy_tag_template must be a string")
        deploy_template = ""
    if not isinstance(arch_template, str):
        errors.append("tag_policy.arch_tag_template must be a string")
        arch_template = ""

    deploy_fields = template_fields(deploy_template)
    arch_fields = template_fields(arch_template)
    if deploy_fields != REQUIRED_TEMPLATE_FIELDS:
        errors.append(
            "deploy_tag_template must use only "
            f"{sorted(REQUIRED_TEMPLATE_FIELDS)} fields"
        )
    if arch_fields != ARCH_TEMPLATE_FIELDS:
        errors.append(
            "arch_tag_template must use only "
            f"{sorted(ARCH_TEMPLATE_FIELDS)} fields"
        )

    if errors:
        return

    release = matrix["release"]
    for distro in distros:
        for arch in architectures:
            deploy_tag = render_tag(deploy_template, release, distro, arch)
            arch_tag = render_tag(arch_template, release, distro, arch)
            if deploy_tag.endswith(f"-{arch}"):
                errors.append(f"deploy tag must not include arch suffix: {deploy_tag}")
            if not arch_tag.endswith(f"-{arch}"):
                errors.append(f"arch tag must include arch suffix: {arch_tag}")


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
        if profile.get("schema_version") != 2:
            errors.append(f"{profile_path.relative_to(ROOT)} schema_version must be 2")
        if profile.get("name") != profile_name:
            errors.append(f"{profile_path.relative_to(ROOT)} name must be {profile_name!r}")

        images = profile.get("images")
        if not isinstance(images, list) or not images:
            errors.append(f"{profile_path.relative_to(ROOT)} images must be a non-empty list")
            continue

        image_names: set[str] = set()
        kolla_variables: set[str] = set()
        for index, image in enumerate(images):
            if not isinstance(image, dict):
                errors.append(
                    f"{profile_path.relative_to(ROOT)} images[{index}] must be an object"
                )
                continue

            name = image.get("name")
            if not isinstance(name, str) or not IMAGE_NAME_RE.fullmatch(name):
                errors.append(
                    f"{profile_path.relative_to(ROOT)} images[{index}].name "
                    "must be a Kolla image name"
                )
            elif name in image_names:
                errors.append(
                    f"{profile_path.relative_to(ROOT)} duplicate image name: {name}"
                )
            else:
                image_names.add(name)

            variables = image.get("kolla_ansible_variables")
            if not isinstance(variables, list) or not variables:
                errors.append(
                    f"{profile_path.relative_to(ROOT)} images[{index}]."
                    "kolla_ansible_variables must be a non-empty list"
                )
                continue

            for variable in variables:
                if not isinstance(variable, str) or not KOLLA_IMAGE_VARIABLE_RE.fullmatch(
                    variable
                ):
                    errors.append(
                        f"{profile_path.relative_to(ROOT)} images[{index}] "
                        f"has invalid Kolla-Ansible variable: {variable!r}"
                    )
                    continue
                if variable in kolla_variables:
                    errors.append(
                        f"{profile_path.relative_to(ROOT)} duplicate "
                        f"Kolla-Ansible variable: {variable}"
                    )
                    continue
                kolla_variables.add(variable)

        build_groups = profile.get("build_groups")
        if not isinstance(build_groups, list) or not build_groups:
            errors.append(
                f"{profile_path.relative_to(ROOT)} build_groups must be a non-empty list"
            )
            continue

        build_group_names: set[str] = set()
        grouped_images: set[str] = set()
        for index, build_group in enumerate(build_groups):
            if not isinstance(build_group, dict):
                errors.append(
                    f"{profile_path.relative_to(ROOT)} build_groups[{index}] must be an object"
                )
                continue

            group_name = build_group.get("name")
            if not isinstance(group_name, str) or not BUILD_GROUP_NAME_RE.fullmatch(
                group_name
            ):
                errors.append(
                    f"{profile_path.relative_to(ROOT)} build_groups[{index}].name "
                    "must be a build group name"
                )
            elif group_name in build_group_names:
                errors.append(
                    f"{profile_path.relative_to(ROOT)} duplicate build group: {group_name}"
                )
            else:
                build_group_names.add(group_name)

            parent = build_group.get("parent")
            if not isinstance(parent, str) or not IMAGE_NAME_RE.fullmatch(parent):
                errors.append(
                    f"{profile_path.relative_to(ROOT)} build_groups[{index}].parent "
                    "must be a Kolla image name"
                )

            parents = build_group.get("parents")
            if parents is not None:
                if not isinstance(parents, list) or not parents:
                    errors.append(
                        f"{profile_path.relative_to(ROOT)} build_groups[{index}].parents "
                        "must be a non-empty list"
                    )
                else:
                    seen_parents: set[str] = set()
                    for chain_parent in parents:
                        if not isinstance(chain_parent, str) or not IMAGE_NAME_RE.fullmatch(
                            chain_parent
                        ):
                            errors.append(
                                f"{profile_path.relative_to(ROOT)} build_groups[{index}] "
                                f"has invalid parent chain image: {chain_parent!r}"
                            )
                        elif chain_parent in seen_parents:
                            errors.append(
                                f"{profile_path.relative_to(ROOT)} build_groups[{index}] "
                                f"duplicates parent chain image: {chain_parent}"
                            )
                        else:
                            seen_parents.add(chain_parent)
                    if isinstance(parent, str) and parents[-1] != parent:
                        errors.append(
                            f"{profile_path.relative_to(ROOT)} build_groups[{index}].parents "
                            "must end with build_groups[].parent"
                        )

            group_images = build_group.get("images")
            if not isinstance(group_images, list) or not group_images:
                errors.append(
                    f"{profile_path.relative_to(ROOT)} build_groups[{index}].images "
                    "must be a non-empty list"
                )
                continue

            for image in group_images:
                if image not in image_names:
                    errors.append(
                        f"{profile_path.relative_to(ROOT)} build group {group_name!r} "
                        f"references unknown image: {image!r}"
                    )
                    continue
                if image in grouped_images:
                    errors.append(
                        f"{profile_path.relative_to(ROOT)} image appears in multiple "
                        f"build groups: {image}"
                    )
                    continue
                grouped_images.add(image)

        missing_group_images = sorted(image_names - grouped_images)
        for image in missing_group_images:
            errors.append(
                f"{profile_path.relative_to(ROOT)} image is not assigned to a build group: {image}"
            )


def main() -> int:
    errors: list[str] = []
    matrix = load_json(MATRIX_PATH)
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
