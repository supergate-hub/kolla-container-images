"""Shared authorization requirements for the three GHCR publish scopes."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AuthorizationRequirement:
    scope: str
    variable: str


_VARIABLE_BY_SCOPE = {
    ("core", "keystone"): "ALLOW_GHCR_PUBLISH",
    ("core", "all"): "ALLOW_GHCR_FULL_CORE_PUBLISH",
    ("deployment", "all"): "ALLOW_GHCR_DEPLOYMENT_PUBLISH",
}

_SELECTION_BY_SCOPE = {
    "keystone": ("core", "keystone"),
    "core": ("core", "all"),
    "deployment": ("deployment", "all"),
}


def scope_selection(scope: str) -> tuple[str, str]:
    """Map one Actions scope choice to the planner's profile/image selection."""
    try:
        return _SELECTION_BY_SCOPE[scope]
    except (KeyError, TypeError) as exc:
        raise ValueError(
            "publish scope must be one of: keystone, core, deployment"
        ) from exc


def authorization_requirement(
    profile: str,
    image: str,
) -> AuthorizationRequirement | None:
    """Return the kill switch required for one supported frozen-plan scope."""
    variable = _VARIABLE_BY_SCOPE.get((profile, image))
    if variable is None:
        return None
    for scope, selection in _SELECTION_BY_SCOPE.items():
        if selection == (profile, image):
            return AuthorizationRequirement(scope=scope, variable=variable)
    raise AssertionError("publish scope maps are inconsistent")
