"""Shared approval requirements for GHCR publish scopes."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ApprovalRequirement:
    variable: str
    phrase: str


_VARIABLE_BY_SCOPE = {
    ("core", "keystone"): "ALLOW_GHCR_PUBLISH",
    ("core", "all"): "ALLOW_GHCR_FULL_CORE_PUBLISH",
    ("deployment", "all"): "ALLOW_GHCR_DEPLOYMENT_PUBLISH",
}


def approval_requirement(
    registry_path: str,
    stream: str,
    profile: str,
    image: str,
    image_count: int,
) -> ApprovalRequirement | None:
    variable = _VARIABLE_BY_SCOPE.get((profile, image))
    if variable is None:
        return None
    noun = "image" if image_count == 1 else "images"
    return ApprovalRequirement(
        variable=variable,
        phrase=(
            f"PUBLISH {registry_path} {stream} {profile}/{image} "
            f"({image_count} {noun}, amd64/arm64)"
        ),
    )
