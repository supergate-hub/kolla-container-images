#!/usr/bin/env python3
"""Validate Kolla image publish summary artifacts."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from profile_resolver import (
    find_stream,
    load_matrix,
    load_profile,
    render_candidate_tag,
    resolve_profile,
    validate_candidate_id,
)


ROOT = Path(__file__).resolve().parents[1]
DIGEST_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
SUMMARY_KEYS = frozenset(
    {
        "candidate_id",
        "stream",
        "release",
        "distro",
        "distro_version",
        "profile",
        "scope",
        "registry",
        "owner",
        "repository",
        "images",
    }
)
IMAGE_KEYS = frozenset(
    {
        "image",
        "kolla_ansible_variables",
        "deploy_tag",
        "deploy_ref",
        "manifest_digest",
        "architectures",
    }
)
ARCHITECTURE_KEYS = frozenset({"arch", "platform", "arch_ref", "digest"})


def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as file_obj:
        return json.load(file_obj, object_pairs_hook=reject_duplicate_keys)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate a Kolla publish summary JSON file.")
    parser.add_argument("--publish-summary", required=True, type=Path)
    parser.add_argument("--stream", required=True, help="Build stream ID")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help="Allow a summary for one selected image instead of the full profile.",
    )
    parser.add_argument("--image", help="Expected image when --allow-partial is used")
    return parser.parse_args()


def image_ref(registry: str, owner: str, repository: str, image: str, tag: str) -> str:
    return f"{registry}/{owner}/{repository}/{image}:{tag}"


def exact_mapping(actual: Any, expected: dict[str, Any]) -> bool:
    if type(actual) is not dict or set(actual) != set(expected):
        return False
    return all(
        type(actual[key]) is type(expected_value) and actual[key] == expected_value
        for key, expected_value in expected.items()
    )


def validate_exact_keys(
    actual: dict[str, Any],
    expected: frozenset[str],
    label: str,
) -> list[str]:
    actual_keys = set(actual)
    if actual_keys == expected:
        return []

    details = []
    missing = sorted(expected - actual_keys)
    unexpected = sorted(actual_keys - expected)
    if missing:
        details.append(f"missing {missing!r}")
    if unexpected:
        details.append(f"unexpected {unexpected!r}")
    return [
        f"{label} keys must be exactly {sorted(expected)!r}; "
        + "; ".join(details)
    ]


def validate_scope(
    summary: dict[str, Any],
    matrix: dict[str, Any],
    profile: dict[str, Any],
    stream: dict[str, Any],
    image_filter: str | None,
    image_count: int,
    candidate_id: str,
) -> list[str]:
    expected_identity = {
        "candidate_id": candidate_id,
        "stream": stream["id"],
        "release": stream["release"],
        "distro": stream["distro"],
        "distro_version": stream["base_tag"],
        "profile": profile["name"],
        "registry": matrix["registry"],
        "owner": matrix["owner"],
        "repository": matrix["repository"],
    }
    errors: list[str] = []
    for key, expected_value in expected_identity.items():
        actual = summary.get(key)
        if type(actual) is not type(expected_value) or actual != expected_value:
            errors.append(f"publish summary {key} must be {expected_value!r}, got {actual!r}")

    expected_scope = {
        "profile": profile["name"],
        "image": image_filter or "all",
        "image_count": image_count,
    }
    actual_scope = summary.get("scope")
    if not exact_mapping(actual_scope, expected_scope):
        errors.append(
            f"publish summary scope must be {expected_scope!r}, got {actual_scope!r}"
        )
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
    if profile["name"] != "core" or image_filter != "keystone":
        raise ValueError(
            "partial publish summaries are only supported for core/keystone"
        )
    return {image_filter: images[image_filter]}


def summary_images(summary: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    images = summary.get("images")
    if type(images) is not list:
        return {}, ["publish summary images must be a list"]

    result: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for index, image_summary in enumerate(images):
        if type(image_summary) is not dict:
            errors.append(f"publish summary images[{index}] must be an object")
            continue
        errors.extend(
            validate_exact_keys(
                image_summary,
                IMAGE_KEYS,
                f"publish summary images[{index}]",
            )
        )
        image = image_summary.get("image")
        if type(image) is not str or not image:
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
    stream: dict[str, Any],
    candidate_id: str,
) -> list[str]:
    errors: list[str] = []
    deploy_tag = render_candidate_tag(matrix, stream, candidate_id)
    expected_ref = image_ref(
        matrix["registry"],
        matrix["owner"],
        matrix["repository"],
        image,
        deploy_tag,
    )
    deploy_ref = image_summary.get("deploy_ref")
    if type(deploy_ref) is not str or deploy_ref != expected_ref:
        errors.append(f"{image} deploy_ref must be {expected_ref!r}")

    actual_deploy_tag = image_summary.get("deploy_tag")
    if type(actual_deploy_tag) is not str or actual_deploy_tag != deploy_tag:
        errors.append(f"{image} deploy_tag must be {deploy_tag!r}")

    variables = image_summary.get("kolla_ansible_variables")
    if (
        type(variables) is not list
        or variables != expected_profile_image["kolla_ansible_variables"]
    ):
        errors.append(f"{image} kolla_ansible_variables do not match profile")

    manifest_digest = image_summary.get("manifest_digest")
    if type(manifest_digest) is not str or not DIGEST_RE.fullmatch(manifest_digest):
        errors.append(f"{image} manifest_digest must be sha256:<64 hex chars>")

    architectures = image_summary.get("architectures")
    expected_arches = matrix["architectures"]
    if type(architectures) is not list:
        errors.append(f"{image} architectures must be exactly {expected_arches!r}")
        return errors

    architectures_by_name: dict[str, dict[str, Any]] = {}
    for index, architecture in enumerate(architectures):
        if type(architecture) is not dict:
            errors.append(f"{image} architectures[{index}] must be an object")
            continue
        errors.extend(
            validate_exact_keys(
                architecture,
                ARCHITECTURE_KEYS,
                f"{image} architectures[{index}]",
            )
        )
        arch = architecture.get("arch")
        if type(arch) is not str or not arch:
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
        expected_arch_ref = image_ref(
            matrix["registry"],
            matrix["owner"],
            matrix["repository"],
            image,
            render_candidate_tag(matrix, stream, candidate_id, arch),
        )
        arch_ref = architecture.get("arch_ref")
        if type(arch_ref) is not str or arch_ref != expected_arch_ref:
            errors.append(f"{image} {arch} arch_ref must be {expected_arch_ref!r}")
        expected_platform = f"linux/{arch}"
        platform = architecture.get("platform")
        if type(platform) is not str or platform != expected_platform:
            errors.append(f"{image} {arch} platform must be {expected_platform!r}")
        child_digest = architecture.get("digest")
        if type(child_digest) is not str or not DIGEST_RE.fullmatch(child_digest):
            errors.append(f"{image} {arch} digest must be sha256:<64 hex chars>")

    return errors


def validate_publish_summary(
    matrix: dict[str, Any],
    profile: dict[str, Any],
    stream: dict[str, Any],
    summary: dict[str, Any],
    allow_partial: bool,
    image_filter: str | None,
    candidate_id: str,
) -> list[str]:
    candidate_id = validate_candidate_id(candidate_id)
    expected_images = selected_profile_images(profile, allow_partial, image_filter)
    if type(summary) is not dict:
        return ["publish summary must be an object"]

    errors = validate_exact_keys(summary, SUMMARY_KEYS, "publish summary")
    actual_images, image_errors = summary_images(summary)
    errors.extend(image_errors)
    errors.extend(
        validate_scope(
            summary,
            matrix,
            profile,
            stream,
            image_filter,
            len(expected_images),
            candidate_id,
        )
    )

    missing = sorted(set(expected_images) - set(actual_images))
    unknown = sorted(set(actual_images) - set(expected_images))
    for image in missing:
        errors.append(f"publish summary is missing image: {image}")
    for image in unknown:
        errors.append(f"publish summary contains unexpected image: {image}")

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
                stream,
                candidate_id,
            )
        )
    return errors


def main() -> int:
    args = parse_args()

    try:
        matrix = load_matrix()
        stream = find_stream(matrix, args.stream)
        profile = resolve_profile(load_profile(args.profile), stream)
        summary = load_json(args.publish_summary)
        errors = validate_publish_summary(
            matrix,
            profile,
            stream,
            summary,
            args.allow_partial,
            args.image,
            args.candidate_id,
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
