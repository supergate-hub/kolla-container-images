"""Shared scope selection for the reviewed GHCR publish surfaces."""

from __future__ import annotations

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
