#!/usr/bin/env python3
"""Validate Kolla image publish summary artifacts."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
MATRIX_PATH = ROOT / "config" / "build-matrix.json"
PROFILES_DIR = ROOT / "config" / "profiles"
DIGEST_RE = re.compile(r"^sha256:[a-f0-9]{64}$")


def load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as file_obj:
        return json.load(file_obj)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate a Kolla publish summary JSON file.")
    parser.add_argument("--publish-summary", required=True, type=Path)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--release", required=True)
    parser.add_argument("--distro", required=True)
    parser.add_argument("--distro-version", required=True)
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help="Allow a summary for one selected image instead of the full profile.",
    )
    parser.add_argument("--image", help="Expected image when --allow-partial is used")
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


def render_tag(
    template: str,
    release: str,
    distro: dict[str, str],
    arch: str | None = None,
) -> str:
    return template.format(
        release=release,
        distro=distro["name"],
        distro_version=distro["version"],
        arch=arch or "",
    )


def image_ref(registry: str, owner: str, repository: str, image: str, tag: str) -> str:
    return f"{registry}/{owner}/{repository}/{image}:{tag}"


def validate_scope(
    summary: dict[str, Any],
    matrix: dict[str, Any],
    profile_name: str,
    release: str,
    distro: dict[str, str],
) -> list[str]:
    expected = {
        "release": release,
        "distro": distro["name"],
        "distro_version": distro["version"],
        "profile": profile_name,
        "registry": matrix["registry"],
        "owner": matrix["owner"],
        "repository": matrix["repository"],
    }
    errors: list[str] = []
    for key, expected_value in expected.items():
        actual = summary.get(key)
        if actual != expected_value:
            errors.append(f"publish summary {key} must be {expected_value!r}, got {actual!r}")
    return errors


def profile_images(profile: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {image["name"]: image for image in profile["images"]}


def selected_profile_images(
    profile: dict[str, Any],
    allow_partial: bool,
    image_filter: str | None,
) -> dict[str, dict[str, Any]]:
    images = profile_images(profile)
    if not allow_partial:
        if image_filter:
            raise ValueError("--image requires --allow-partial")
        return images

    if not image_filter:
        raise ValueError("--allow-partial requires --image")
    if image_filter not in images:
        raise ValueError(f"image does not exist in profile {profile['name']}: {image_filter}")
    return {image_filter: images[image_filter]}


def summary_images(summary: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    images = summary.get("images")
    if not isinstance(images, list):
        return {}, ["publish summary images must be a list"]

    result: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for index, image_summary in enumerate(images):
        if not isinstance(image_summary, dict):
            errors.append(f"publish summary images[{index}] must be an object")
            continue
        image = image_summary.get("image")
        if not isinstance(image, str) or not image:
            errors.append(f"publish summary images[{index}].image must be a string")
            continue
        if image in result:
            errors.append(f"publish summary contains duplicate image: {image}")
            continue
        result[image] = image_summary
    return result, errors


def validate_image(
    image: str,
    expected_profile_image: dict[str, Any],
    image_summary: dict[str, Any],
    matrix: dict[str, Any],
    release: str,
    distro: dict[str, str],
    deploy_tag: str,
) -> list[str]:
    errors: list[str] = []
    expected_ref = image_ref(
        matrix["registry"],
        matrix["owner"],
        matrix["repository"],
        image,
        deploy_tag,
    )
    if image_summary.get("deploy_ref") != expected_ref:
        errors.append(f"{image} deploy_ref must be {expected_ref!r}")

    if image_summary.get("deploy_tag") not in {None, deploy_tag}:
        errors.append(f"{image} deploy_tag must be {deploy_tag!r}")

    variables = image_summary.get("kolla_ansible_variables")
    if variables is not None and variables != expected_profile_image["kolla_ansible_variables"]:
        errors.append(f"{image} kolla_ansible_variables do not match profile")

    digest = image_summary.get("manifest_digest")
    if not isinstance(digest, str) or not DIGEST_RE.fullmatch(digest):
        errors.append(f"{image} manifest_digest must be sha256:<64 hex chars>")

    architectures = image_summary.get("architectures")
    expected_arches = matrix["architectures"]
    if not isinstance(architectures, list):
        errors.append(f"{image} architectures must be exactly {expected_arches!r}")
        return errors

    architectures_by_name: dict[str, dict[str, Any]] = {}
    for index, architecture in enumerate(architectures):
        if not isinstance(architecture, dict):
            errors.append(f"{image} architectures[{index}] must be an object")
            continue
        arch = architecture.get("arch")
        if not isinstance(arch, str) or not arch:
            errors.append(f"{image} architectures[{index}].arch must be a string")
            continue
        if arch in architectures_by_name:
            errors.append(f"{image} contains duplicate architecture: {arch}")
            continue
        architectures_by_name[arch] = architecture

    if set(architectures_by_name) != set(expected_arches):
        errors.append(f"{image} architectures must be exactly {expected_arches!r}")

    for arch in expected_arches:
        architecture = architectures_by_name.get(arch)
        if architecture is None:
            continue
        arch_tag = render_tag(
            matrix["tag_policy"]["arch_tag_template"],
            release,
            distro,
            arch,
        )
        expected_arch_ref = image_ref(
            matrix["registry"],
            matrix["owner"],
            matrix["repository"],
            image,
            arch_tag,
        )
        if architecture.get("arch_ref") != expected_arch_ref:
            errors.append(f"{image} {arch} arch_ref must be {expected_arch_ref!r}")
        expected_platform = f"linux/{arch}"
        if architecture.get("platform") != expected_platform:
            errors.append(f"{image} {arch} platform must be {expected_platform!r}")

    return errors


def validate_publish_summary(
    matrix: dict[str, Any],
    profile: dict[str, Any],
    release: str,
    distro: dict[str, str],
    summary: dict[str, Any],
    allow_partial: bool,
    image_filter: str | None,
) -> list[str]:
    expected_images = selected_profile_images(profile, allow_partial, image_filter)
    actual_images, errors = summary_images(summary)
    errors.extend(validate_scope(summary, matrix, profile["name"], release, distro))

    missing = sorted(set(expected_images) - set(actual_images))
    unknown = sorted(set(actual_images) - set(expected_images))
    for image in missing:
        errors.append(f"publish summary is missing image: {image}")
    for image in unknown:
        errors.append(f"publish summary contains unexpected image: {image}")

    deploy_tag = render_tag(matrix["tag_policy"]["deploy_tag_template"], release, distro)
    for image, expected_profile_image in expected_images.items():
        image_summary = actual_images.get(image)
        if image_summary is None:
            continue
        errors.extend(
            validate_image(
                image,
                expected_profile_image,
                image_summary,
                matrix,
                release,
                distro,
                deploy_tag,
            )
        )
    return errors


def main() -> int:
    args = parse_args()
    errors: list[str] = []

    try:
        matrix = load_json(MATRIX_PATH)
        if args.release != matrix["release"]:
            raise ValueError(f"unsupported release: {args.release}")
        distro = find_distro(matrix, args.distro, args.distro_version)
        profile = load_profile(args.profile)
        summary = load_json(args.publish_summary)
        errors = validate_publish_summary(
            matrix,
            profile,
            args.release,
            distro,
            summary,
            args.allow_partial,
            args.image,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if errors:
        print("Publish summary validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    print("Publish summary validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
