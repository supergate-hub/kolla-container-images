#!/usr/bin/env python3
"""Validate the explicit approval gate for a real GHCR publish."""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class PublishScope:
    profile: str
    image: str
    release: str
    distro: str
    distro_version: str


SMOKE_SCOPE = PublishScope("core", "keystone", "2025.1", "rocky", "9")
FULL_CORE_SCOPE = PublishScope("core", "all", "2025.1", "rocky", "9")
DEPLOYMENT_SCOPE = PublishScope("deployment", "all", "2025.1", "rocky", "9")

SMOKE_APPROVAL = (
    "I approve GHCR smoke publish for keystone 2025.1-rocky-9 from "
    "supergate-jhbyun/kolla-container-images."
)
FULL_CORE_APPROVAL = (
    "I approve GHCR full-core publish for core 2025.1-rocky-9 "
    "(21 images, amd64/arm64) from supergate-jhbyun/kolla-container-images."
)
DEPLOYMENT_APPROVAL = (
    "I approve GHCR deployment publish for deployment 2025.1-rocky-9 "
    "(52 images, amd64/arm64) from supergate-jhbyun/kolla-container-images."
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--release", required=True)
    parser.add_argument("--distro", required=True)
    parser.add_argument("--distro-version", required=True)
    return parser.parse_args()


def reject(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


def main() -> int:
    args = parse_args()
    scope = PublishScope(
        args.profile,
        args.image,
        args.release,
        args.distro,
        args.distro_version,
    )
    approval = os.environ.get("APPROVAL", "")

    if scope == SMOKE_SCOPE:
        if os.environ.get("ALLOW_GHCR_PUBLISH") != "true":
            return reject("Smoke publish requires ALLOW_GHCR_PUBLISH=true.")
        if approval != SMOKE_APPROVAL:
            return reject("Smoke publish requires the exact smoke publish approval phrase.")
        print("Smoke publish approval validated.")
        return 0

    if scope == FULL_CORE_SCOPE:
        if os.environ.get("ALLOW_GHCR_FULL_CORE_PUBLISH") != "true":
            return reject(
                "Full-core publish requires ALLOW_GHCR_FULL_CORE_PUBLISH=true."
            )
        if approval != FULL_CORE_APPROVAL:
            return reject(
                "Full-core publish requires the exact full-core publish approval phrase."
            )
        print("Full-core publish approval validated.")
        return 0

    if scope == DEPLOYMENT_SCOPE:
        if os.environ.get("ALLOW_GHCR_DEPLOYMENT_PUBLISH") != "true":
            return reject(
                "Deployment publish requires ALLOW_GHCR_DEPLOYMENT_PUBLISH=true."
            )
        if approval != DEPLOYMENT_APPROVAL:
            return reject(
                "Deployment publish requires the exact deployment publish approval phrase."
            )
        print("Deployment publish approval validated.")
        return 0

    return reject(
        "Requested scope is not approved for real publish: "
        f"{scope.profile}/{scope.image} "
        f"{scope.release}-{scope.distro}-{scope.distro_version}."
    )


if __name__ == "__main__":
    raise SystemExit(main())
