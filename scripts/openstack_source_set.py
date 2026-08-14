from __future__ import annotations

import ast
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


SOURCE_SET_KEYS = {
    "schema_version",
    "id",
    "release",
    "series",
    "policy",
    "generated_at",
    "projects",
}
SOURCE_SET_V2_KEYS = SOURCE_SET_KEYS | {"direct_artifacts"}
SOURCE_SET_V3_KEYS = SOURCE_SET_V2_KEYS | {"kolla_source_inputs"}
PROJECT_KEYS = {
    "repository",
    "track_ref",
    "build_commit",
    "kolla_sections",
    "nearest_release",
}
REQUIREMENTS_KEYS = PROJECT_KEYS | {"upper_constraints_sha256"}
DIRECT_ARTIFACT_KEYS = {
    "repository",
    "commit",
    "path",
    "url",
    "sha256",
    "kolla_sections",
}
KOLLA_SOURCE_INPUT_KEYS = {"kolla", "kolla_ansible"}
KOLLA_PIN_KEYS = {
    "repository",
    "commit",
    "sources_sha256",
    "closure_sha256",
}
KOLLA_ANSIBLE_PIN_KEYS = {"repository", "commit"}
KOLLA_REPOSITORY = "https://opendev.org/openstack/kolla"
KOLLA_ANSIBLE_REPOSITORY = "https://opendev.org/openstack/kolla-ansible"
DIRECT_ARTIFACT_SPECS = {
    "mariadb-clustercheck": {
        "repository": "https://src.fedoraproject.org/rpms/mariadb",
        "track_ref": "10.9",
        "path": "f/clustercheck.sh",
        "kolla_sections": ["mariadb-base"],
        "url_template": (
            "https://src.fedoraproject.org/rpms/mariadb/raw/"
            "{commit}/f/clustercheck.sh"
        ),
    },
    "ovn-ctl": {
        "repository": "https://github.com/ovn-org/ovn",
        "track_ref": "main",
        "path": "utilities/ovn-ctl",
        "kolla_sections": ["ovn-sb-db-relay"],
        "url_template": (
            "https://raw.githubusercontent.com/ovn-org/ovn/"
            "{commit}/utilities/ovn-ctl"
        ),
    },
}
KOLLA_TEMPLATE_SEAMS = {
    "constraints": {
        "path": "docker/kolla-toolbox/Dockerfile.j2",
        "fragments": (
            "{% block kolla_toolbox_pip_conf %}",
            "{% block kolla_toolbox_upper_constraints %}",
            '{{ macros.upper_constraints_remove("openstacksdk") }}',
            "python3 -m venv --system-site-packages {{ venv_path }}",
        ),
    },
    "mariadb-clustercheck": {
        "path": "docker/mariadb/mariadb-base/Dockerfile.j2",
        "fragments": (
            "{% block mariadb_clustercheck_version %}",
            "${mariadb_clustercheck_url}",
            "/usr/bin/clustercheck",
            "{% block mariadb_base_footer %}",
        ),
    },
    "ovn-ctl": {
        "path": "docker/ovn/ovn-sb-db-relay/Dockerfile.j2",
        "fragments": (
            "{% block ovn_sb_db_relay_ovn_ctl %}",
            "/usr/share/ovn/scripts/ovn-ctl",
        ),
    },
}
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ID_RE = re.compile(r"^[a-z][a-z0-9-]*$")
RELEASE_RE = re.compile(r"^[0-9]{4}\.[0-9]+$")
SERIES_RE = re.compile(r"^[a-z][a-z0-9-]*$")
VERSION_RE = re.compile(r"^[1-9][0-9]*\.[0-9]+\.[0-9]+(?:\.[0-9A-Za-z]+)*$")
PROJECT_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
TIMESTAMP_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"
)


class OpenStackSourceSetError(ValueError):
    """Raised when a source-set cannot prove immutable build inputs."""


@dataclass(frozen=True)
class OpenStackSourceSet:
    document: dict[str, Any]
    canonical_json: str
    sha256: str


@dataclass(frozen=True)
class KollaSourceInput:
    version: str
    kolla_repository: str
    kolla_commit: str
    kolla_ansible_repository: str
    kolla_ansible_commit: str
    sources_path: Path


@dataclass(frozen=True)
class FrozenKollaSources:
    config_content: str
    config_sha256: str
    template_override_content: str
    template_override_sha256: str
    source_sections: tuple[str, ...]
    project_names: tuple[str, ...]


FROZEN_CONTRACT_KEYS = {
    "source_set",
    "canonical_digest",
    "kolla_build_config",
    "template_override",
}
FROZEN_FILE_KEYS = {"sha256", "content"}


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise OpenStackSourceSetError(f"duplicate JSON key: {key!r}")
        value[key] = item
    return value


def canonical_source_set_json(document: dict[str, Any]) -> str:
    return json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _validate_project(
    name: str, value: Any, *, expected_release: str
) -> None:
    context = f"projects.{name}"
    if not isinstance(name, str) or not PROJECT_RE.fullmatch(name):
        raise OpenStackSourceSetError(f"invalid project name: {name!r}")
    expected_keys = REQUIREMENTS_KEYS if name == "openstack/requirements" else PROJECT_KEYS
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise OpenStackSourceSetError(
            f"{context} keys must be exactly {sorted(expected_keys)!r}"
        )
    expected_repository = (
        f"https://opendev.org/{name}"
        if name.startswith("openstack/")
        else f"https://github.com/{name}"
    )
    if value["repository"] != expected_repository:
        raise OpenStackSourceSetError(
            f"{context}.repository must be {expected_repository!r}"
        )
    expected_track_ref = (
        f"stable/{expected_release}" if name.startswith("openstack/") else "master"
    )
    if value["track_ref"] != expected_track_ref:
        raise OpenStackSourceSetError(
            f"{context}.track_ref must be {expected_track_ref!r}"
        )
    if not isinstance(value["build_commit"], str) or not SHA_RE.fullmatch(
        value["build_commit"]
    ):
        raise OpenStackSourceSetError(
            f"{context}.build_commit must be a lowercase 40-character SHA"
        )
    sections = value["kolla_sections"]
    if (
        not isinstance(sections, list)
        or not sections
        or sections != sorted(sections)
        or len(sections) != len(set(sections))
        or any(
            not isinstance(section, str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", section)
            for section in sections
        )
    ):
        raise OpenStackSourceSetError(
            f"{context}.kolla_sections must be a sorted unique non-empty list"
        )
    nearest_release = value["nearest_release"]
    if (
        name == "openstack/requirements" or not name.startswith("openstack/")
    ) and nearest_release is not None:
        raise OpenStackSourceSetError(f"{context}.nearest_release must be null")
    if nearest_release is not None:
        if (
            not isinstance(nearest_release, dict)
            or set(nearest_release) != {"version", "commit"}
            or not isinstance(nearest_release["version"], str)
            or not nearest_release["version"]
            or not isinstance(nearest_release["commit"], str)
            or not SHA_RE.fullmatch(nearest_release["commit"])
        ):
            raise OpenStackSourceSetError(
                f"{context}.nearest_release must be null or an exact version/commit pair"
            )
    if name == "openstack/requirements":
        constraints_sha256 = value["upper_constraints_sha256"]
        if not isinstance(constraints_sha256, str) or not SHA256_RE.fullmatch(
            constraints_sha256
        ):
            raise OpenStackSourceSetError(
                f"{context}.upper_constraints_sha256 must be a lowercase SHA-256"
            )


def _validate_direct_artifacts(value: Any, *, series: str) -> None:
    expected_names = {"ovn-ctl"}
    if series == "epoxy":
        expected_names.add("mariadb-clustercheck")
    if not isinstance(value, dict) or set(value) != expected_names:
        raise OpenStackSourceSetError(
            "source-set direct_artifacts must contain exactly "
            f"{sorted(expected_names)!r} for series {series!r}"
        )
    for name, artifact in value.items():
        context = f"direct_artifacts.{name}"
        if not isinstance(artifact, dict) or set(artifact) != DIRECT_ARTIFACT_KEYS:
            raise OpenStackSourceSetError(
                f"{context} keys must be exactly {sorted(DIRECT_ARTIFACT_KEYS)!r}"
            )
        spec = DIRECT_ARTIFACT_SPECS[name]
        for key in ("repository", "path", "kolla_sections"):
            if artifact[key] != spec[key]:
                raise OpenStackSourceSetError(
                    f"{context}.{key} must be {spec[key]!r}"
                )
        commit = artifact["commit"]
        if not isinstance(commit, str) or not SHA_RE.fullmatch(commit):
            raise OpenStackSourceSetError(
                f"{context}.commit must be a lowercase 40-character SHA"
            )
        expected_url = spec["url_template"].format(commit=commit)
        if artifact["url"] != expected_url:
            raise OpenStackSourceSetError(
                f"{context}.url must be the commit-addressed upstream URL"
            )
        if not isinstance(artifact["sha256"], str) or not SHA256_RE.fullmatch(
            artifact["sha256"]
        ):
            raise OpenStackSourceSetError(
                f"{context}.sha256 must be a lowercase SHA-256"
            )


def _validate_kolla_source_inputs(value: Any) -> None:
    if not isinstance(value, dict) or not value:
        raise OpenStackSourceSetError(
            "source-set kolla_source_inputs must be a non-empty object"
        )
    for version, toolchain in value.items():
        context = f"kolla_source_inputs.{version}"
        if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
            raise OpenStackSourceSetError(
                f"{context} key must be a Kolla toolchain version"
            )
        if not isinstance(toolchain, dict) or set(toolchain) != KOLLA_SOURCE_INPUT_KEYS:
            raise OpenStackSourceSetError(
                f"{context} keys must be exactly {sorted(KOLLA_SOURCE_INPUT_KEYS)!r}"
            )
        for project, expected_repository, expected_keys in (
            ("kolla", KOLLA_REPOSITORY, KOLLA_PIN_KEYS),
            (
                "kolla_ansible",
                KOLLA_ANSIBLE_REPOSITORY,
                KOLLA_ANSIBLE_PIN_KEYS,
            ),
        ):
            pin = toolchain[project]
            pin_context = f"{context}.{project}"
            if not isinstance(pin, dict) or set(pin) != expected_keys:
                raise OpenStackSourceSetError(
                    f"{pin_context} keys must be exactly {sorted(expected_keys)!r}"
                )
            if pin["repository"] != expected_repository:
                raise OpenStackSourceSetError(
                    f"{pin_context}.repository must be {expected_repository!r}"
                )
            if not isinstance(pin["commit"], str) or not SHA_RE.fullmatch(
                pin["commit"]
            ):
                raise OpenStackSourceSetError(
                    f"{pin_context}.commit must be a lowercase 40-character SHA"
                )
        for digest_name in ("sources_sha256", "closure_sha256"):
            digest = toolchain["kolla"][digest_name]
            if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
                raise OpenStackSourceSetError(
                    f"{context}.kolla.{digest_name} must be a lowercase SHA-256"
                )


def _kolla_source_closure_sha256(
    projects: dict[str, dict[str, Any]],
) -> str:
    closure = {
        project_name: {
            "repository": project["repository"],
            "track_ref": project["track_ref"],
            "kolla_sections": project["kolla_sections"],
        }
        for project_name, project in projects.items()
    }
    canonical = json.dumps(closure, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _snapshot_direct_artifacts(
    series: str,
    *,
    resolve_git_ref: Callable[[str, str], str],
    read_artifact: Callable[[str], bytes],
) -> dict[str, dict[str, Any]]:
    names = {"ovn-ctl"}
    if series == "epoxy":
        names.add("mariadb-clustercheck")
    artifacts: dict[str, dict[str, Any]] = {}
    for name in sorted(names):
        spec = DIRECT_ARTIFACT_SPECS[name]
        commit = resolve_git_ref(spec["repository"], spec["track_ref"])
        if not isinstance(commit, str) or not SHA_RE.fullmatch(commit):
            raise OpenStackSourceSetError(
                f"resolved direct artifact ref for {name!r} is not a full commit SHA"
            )
        url = spec["url_template"].format(commit=commit)
        content = read_artifact(url)
        if not isinstance(content, bytes) or not content:
            raise OpenStackSourceSetError(
                f"direct artifact {name!r} must resolve to non-empty bytes"
            )
        artifacts[name] = {
            "repository": spec["repository"],
            "commit": commit,
            "path": spec["path"],
            "url": url,
            "sha256": hashlib.sha256(content).hexdigest(),
            "kolla_sections": list(spec["kolla_sections"]),
        }
    return artifacts


def validate_source_set_document(
    document: Any,
    *,
    expected_id: str | None = None,
    expected_release: str | None = None,
    expected_series: str | None = None,
) -> OpenStackSourceSet:
    if not isinstance(document, dict):
        raise OpenStackSourceSetError("source-set must be an object")
    schema_version = document.get("schema_version")
    if type(schema_version) is not int or schema_version not in (1, 2, 3):
        raise OpenStackSourceSetError("source-set schema_version must be 1, 2, or 3")
    expected_keys = {
        1: SOURCE_SET_KEYS,
        2: SOURCE_SET_V2_KEYS,
        3: SOURCE_SET_V3_KEYS,
    }[schema_version]
    if set(document) != expected_keys:
        raise OpenStackSourceSetError(
            f"source-set keys must be exactly {sorted(expected_keys)!r}"
        )
    source_set_id = document["id"]
    release = document["release"]
    series = document["series"]
    if not isinstance(source_set_id, str) or not ID_RE.fullmatch(source_set_id):
        raise OpenStackSourceSetError("source-set id is invalid")
    if not isinstance(release, str) or not RELEASE_RE.fullmatch(release):
        raise OpenStackSourceSetError("source-set release is invalid")
    if not isinstance(series, str) or not SERIES_RE.fullmatch(series):
        raise OpenStackSourceSetError("source-set series is invalid")
    if schema_version >= 2:
        _validate_direct_artifacts(document["direct_artifacts"], series=series)
    if schema_version == 3:
        _validate_kolla_source_inputs(document["kolla_source_inputs"])
    for label, actual, expected in (
        ("id", source_set_id, expected_id),
        ("release", release, expected_release),
        ("series", series, expected_series),
    ):
        if expected is not None and actual != expected:
            raise OpenStackSourceSetError(
                f"source-set {label} {actual!r} does not match {expected!r}"
            )
    if document["policy"] != "stable-head-snapshot":
        raise OpenStackSourceSetError(
            "source-set policy must be 'stable-head-snapshot'"
        )
    if not isinstance(document["generated_at"], str) or not TIMESTAMP_RE.fullmatch(
        document["generated_at"]
    ):
        raise OpenStackSourceSetError(
            "source-set generated_at must be a second-precision UTC timestamp"
        )
    projects = document["projects"]
    if not isinstance(projects, dict) or not projects:
        raise OpenStackSourceSetError("source-set projects must be a non-empty object")
    if "openstack/requirements" not in projects:
        raise OpenStackSourceSetError(
            "source-set must include openstack/requirements"
        )
    for name, project in projects.items():
        _validate_project(name, project, expected_release=release)
    if schema_version == 3:
        expected_closure_sha256 = _kolla_source_closure_sha256(projects)
        for version, toolchain in document["kolla_source_inputs"].items():
            if toolchain["kolla"]["closure_sha256"] != expected_closure_sha256:
                raise OpenStackSourceSetError(
                    "kolla_source_inputs."
                    f"{version}.kolla.closure_sha256 does not match source-set projects"
                )

    canonical_json = canonical_source_set_json(document)
    digest = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
    return OpenStackSourceSet(
        document=document,
        canonical_json=canonical_json,
        sha256=f"sha256:{digest}",
    )


def load_source_set(
    path: Path,
    *,
    expected_id: str | None = None,
    expected_release: str | None = None,
    expected_series: str | None = None,
) -> OpenStackSourceSet:
    try:
        document = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise OpenStackSourceSetError(
            f"cannot read source-set {path}: {error}"
        ) from error
    source_set = validate_source_set_document(
        document,
        expected_id=expected_id,
        expected_release=expected_release,
        expected_series=expected_series,
    )
    if path.stem != source_set.document["id"]:
        raise OpenStackSourceSetError(
            f"source-set filename {path.name!r} must match its id"
        )
    return source_set


def validate_source_set_toolchain(
    source_set_document: dict[str, Any],
    *,
    version: str,
    toolchain: dict[str, Any],
) -> None:
    """Require an active source-set to record the selected full toolchain pin."""
    source_set = validate_source_set_document(source_set_document)
    if source_set.document["schema_version"] != 3:
        raise OpenStackSourceSetError(
            "active source-set schema_version must be 3"
        )
    recorded = source_set.document["kolla_source_inputs"].get(version)
    if recorded is None:
        raise OpenStackSourceSetError(
            f"source-set does not record toolchain pin {version!r}; "
            "create a new source-set revision"
        )
    for project in ("kolla", "kolla_ansible"):
        expected = toolchain.get(project)
        actual = recorded[project]
        if (
            not isinstance(expected, dict)
            or actual["repository"] != expected.get("repository")
            or actual["commit"] != expected.get("commit")
        ):
            raise OpenStackSourceSetError(
                f"source-set {project} toolchain pin for {version!r} does not "
                "match the matrix; create a new source-set revision"
            )


def _parse_kolla_sources_content(
    content: str, *, source_name: str
) -> dict[str, dict[str, Any]]:
    try:
        module = ast.parse(content, filename=source_name)
    except SyntaxError as error:
        raise OpenStackSourceSetError(
            f"cannot parse pinned Kolla SOURCES from {source_name}: {error}"
        ) from error
    assignments = [
        statement
        for statement in module.body
        if isinstance(statement, ast.Assign)
        and len(statement.targets) == 1
        and isinstance(statement.targets[0], ast.Name)
        and statement.targets[0].id == "SOURCES"
    ]
    if len(assignments) != 1 or len(module.body) != 1:
        raise OpenStackSourceSetError(
            "pinned Kolla source module must contain only one SOURCES literal assignment"
        )
    try:
        sources = ast.literal_eval(assignments[0].value)
    except (ValueError, TypeError, SyntaxError) as error:
        raise OpenStackSourceSetError(
            "pinned Kolla SOURCES must be a safe Python literal"
        ) from error
    if not isinstance(sources, dict) or not sources:
        raise OpenStackSourceSetError("pinned Kolla SOURCES must be a non-empty dict")
    for name, value in sources.items():
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name)
            or not isinstance(value, dict)
            or value.get("type") not in {"git", "url", "local"}
            or not isinstance(value.get("location"), str)
            or not value["location"]
        ):
            raise OpenStackSourceSetError(
                f"pinned Kolla SOURCES entry is invalid: {name!r}"
            )
    return sources


def parse_kolla_sources(path: Path) -> dict[str, dict[str, Any]]:
    """Read Kolla's literal SOURCES assignment without importing its code."""
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise OpenStackSourceSetError(
            f"cannot parse pinned Kolla SOURCES from {path}: {error}"
        ) from error
    return _parse_kolla_sources_content(content, source_name=str(path))


def _project_for_kolla_source(source: dict[str, Any]) -> str | None:
    location = source["location"]
    marker = "$tarballs_base/openstack/"
    if marker in location:
        repository_name = location.split(marker, 1)[1].split("/", 1)[0]
        if not re.fullmatch(r"[a-z0-9_.-]+", repository_name):
            raise OpenStackSourceSetError(
                f"Kolla OpenStack source location is invalid: {location!r}"
            )
        return f"openstack/{repository_name}"
    if source["type"] != "git":
        return None
    match = re.fullmatch(
        r"https://(?:opendev\.org|github\.com)/"
        r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?",
        location,
    )
    if match is None:
        raise OpenStackSourceSetError(
            f"selected Kolla Git source repository is unsupported: {location!r}"
        )
    return match.group(1)


def _validate_embedded_archive(section: str, source: dict[str, Any]) -> None:
    if source["type"] != "url":
        raise OpenStackSourceSetError(
            f"selected Kolla source {section!r} is not mapped to a frozen Git pin"
        )
    version = source.get("version")
    checksums = source.get("sha256")
    if (
        not isinstance(version, str)
        or not version
        or not isinstance(checksums, dict)
        or set(checksums) != {"amd64", "arm64"}
        or any(
            not isinstance(digest, str) or not SHA256_RE.fullmatch(digest)
            for digest in checksums.values()
        )
    ):
        raise OpenStackSourceSetError(
            f"selected external Kolla archive {section!r} must have a version "
            "and amd64/arm64 checksum pins"
        )


def _selected_source_sections(
    sources: dict[str, dict[str, Any]], images: set[str]
) -> tuple[str, ...]:
    if not images or any(
        not isinstance(image, str)
        or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", image)
        for image in images
    ):
        raise OpenStackSourceSetError("Kolla source closure images are invalid")
    return tuple(
        sorted(
            section
            for section in sources
            if any(
                section == image
                or section.startswith(f"{image}-plugin-")
                or section.startswith(f"{image}-additions-")
                for image in images
            )
        )
    )


def _source_section_bindings(
    source_set_document: dict[str, Any],
) -> dict[str, tuple[str, dict[str, Any]]]:
    bindings: dict[str, tuple[str, dict[str, Any]]] = {}
    for project_name, project in source_set_document["projects"].items():
        for section in project["kolla_sections"]:
            if section in bindings:
                raise OpenStackSourceSetError(
                    f"Kolla source section {section!r} is bound to multiple projects"
                )
            bindings[section] = (project_name, project)
    return bindings


def source_archive_name(section: str) -> str:
    if not isinstance(section, str) or not ID_RE.fullmatch(section):
        raise OpenStackSourceSetError(
            f"source archive Kolla section is invalid: {section!r}"
        )
    return f"{section}.tar"


def _validate_kolla_template_override_seams(
    kolla_sources_path: Path,
    source_set_document: dict[str, Any],
    images: set[str],
) -> None:
    if source_set_document["schema_version"] < 2:
        return
    checkout = kolla_sources_path.parents[2]
    seam_names: list[str] = []
    if "kolla-toolbox" in images:
        seam_names.append("constraints")
    seam_names.extend(
        name
        for name, artifact in source_set_document["direct_artifacts"].items()
        if set(artifact["kolla_sections"]) & images
    )
    for name in seam_names:
        seam = KOLLA_TEMPLATE_SEAMS[name]
        path = checkout / seam["path"]
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise OpenStackSourceSetError(
                f"cannot read pinned Kolla template for {name!r}: {path}: {error}"
            ) from error
        missing = [
            fragment for fragment in seam["fragments"] if fragment not in content
        ]
        if missing:
            raise OpenStackSourceSetError(
                f"pinned Kolla template for {name!r} lacks required override seams: "
                f"{missing!r}"
            )


def render_frozen_configs(
    source_set_document: dict[str, Any],
) -> FrozenKollaSources:
    """Render deterministic build inputs from a validated source-set."""
    source_set = validate_source_set_document(source_set_document)
    bindings = _source_section_bindings(source_set.document)
    blocks: list[str] = []
    used_projects: set[str] = set()
    for section in sorted(bindings):
        project_name, project = bindings[section]
        blocks.append(
            "\n".join(
                (
                    f"[{section}]",
                    "type = local",
                    "location = $locals_base/artifacts/source-archives/"
                    f"{source_archive_name(section)}",
                )
            )
        )
        used_projects.add(project_name)
    config_content = "\n\n".join(blocks) + "\n"
    config_digest = hashlib.sha256(config_content.encode("utf-8")).hexdigest()
    requirements = source_set.document["projects"]["openstack/requirements"]
    requirements_commit = requirements["build_commit"]
    if source_set.document["schema_version"] >= 2:
        direct_artifacts = source_set.document["direct_artifacts"]
        ovn_ctl = direct_artifacts["ovn-ctl"]
        constraints_are_local = "kolla-toolbox" in bindings
        constraints_location = (
            "/requirements/upper-constraints.txt"
            if constraints_are_local
            else "https://releases.openstack.org/constraints/upper/"
            f"{requirements_commit}"
        )
        template_lines = [
            "{% extends parent_template %}",
            "",
            "{% block kolla_toolbox_pip_conf %}",
            f"ENV UPPER_CONSTRAINTS_FILE={constraints_location}",
            "ENV UPPER_CONSTRAINTS_SHA256="
            f"{requirements['upper_constraints_sha256']}",
            "{% endblock %}",
            "",
            "{% block kolla_toolbox_upper_constraints %}",
        ]
        if constraints_are_local:
            template_lines.extend(
                (
                    "ADD kolla-toolbox-archive /kolla-toolbox-source",
                    "",
                    "RUN ln -s kolla-toolbox-source/* /requirements \\",
                )
            )
        else:
            template_lines.extend(
                (
                    "RUN mkdir -p /requirements \\",
                    "    && curl --fail --show-error --location --retry 3 \\",
                    '        -o /requirements/upper-constraints.txt '
                    '"$UPPER_CONSTRAINTS_FILE" \\',
                )
            )
        template_lines.extend(
            (
                '    && echo "$UPPER_CONSTRAINTS_SHA256  '
                '/requirements/upper-constraints.txt" | sha256sum -c - \\',
                '    && {{ macros.upper_constraints_remove("openstacksdk") }} \\',
                "    && python3 -m venv --system-site-packages {{ venv_path }} \\",
                "    && KOLLA_DISTRO_PYTHON_VERSION=$(/usr/bin/python3 -c "
                '"import sys; print(\'{}.{}\'.format(sys.version_info.major, '
                'sys.version_info.minor))") \\',
                "    && cd {{ venv_path }}/lib \\",
                "    && ln -s python${KOLLA_DISTRO_PYTHON_VERSION} "
                "{{ venv_path }}/lib/python3",
                "{% endblock %}",
                "",
                "{% block ovn_sb_db_relay_ovn_ctl %}",
                "RUN curl --fail --show-error --location --retry 3 \\",
                "        -o /usr/share/ovn/scripts/ovn-ctl \\",
                f"        {ovn_ctl['url']} \\",
                f'    && echo "{ovn_ctl["sha256"]}  '
                '/usr/share/ovn/scripts/ovn-ctl" | sha256sum -c -',
                "{% endblock %}",
            )
        )
        clustercheck = direct_artifacts.get("mariadb-clustercheck")
        if clustercheck is not None:
            template_lines.extend(
                (
                    "",
                    "{% block mariadb_clustercheck_version %}",
                    f"ARG mariadb_clustercheck_url={clustercheck['url']}",
                    f"ARG mariadb_clustercheck_sha256={clustercheck['sha256']}",
                    "{% endblock %}",
                    "",
                    "{% block mariadb_base_footer %}",
                    'RUN echo "${mariadb_clustercheck_sha256}  '
                    '/usr/bin/clustercheck" | sha256sum -c -',
                    "{% endblock %}",
                )
            )
        template_override_content = "\n".join(template_lines) + "\n"
    elif "kolla-toolbox" in bindings:
        template_override_content = ""
    else:
        template_override_content = (
            "{% extends parent_template %}\n\n"
            "{% block kolla_toolbox_pip_conf %}\n"
            "ENV UPPER_CONSTRAINTS_FILE="
            "https://releases.openstack.org/constraints/upper/"
            f"{requirements_commit}\n"
            "{% endblock %}\n"
        )
    template_digest = hashlib.sha256(
        template_override_content.encode("utf-8")
    ).hexdigest()
    return FrozenKollaSources(
        config_content=config_content,
        config_sha256=f"sha256:{config_digest}",
        template_override_content=template_override_content,
        template_override_sha256=f"sha256:{template_digest}",
        source_sections=tuple(sorted(bindings)),
        project_names=tuple(sorted(used_projects)),
    )


def validate_frozen_source_contract(value: Any) -> dict[str, Any]:
    """Validate the complete source provenance copied through the pipeline."""
    if not isinstance(value, dict) or set(value) != FROZEN_CONTRACT_KEYS:
        raise OpenStackSourceSetError(
            f"frozen OpenStack source contract keys must be exactly "
            f"{sorted(FROZEN_CONTRACT_KEYS)!r}"
        )
    source_set = validate_source_set_document(value["source_set"])
    if value["canonical_digest"] != source_set.sha256:
        raise OpenStackSourceSetError(
            "frozen OpenStack source canonical digest does not match its source-set"
        )
    rendered = render_frozen_configs(source_set.document)
    expected_files = {
        "kolla_build_config": {
            "sha256": rendered.config_sha256,
            "content": rendered.config_content,
        },
        "template_override": {
            "sha256": rendered.template_override_sha256,
            "content": rendered.template_override_content,
        },
    }
    for name, expected in expected_files.items():
        actual = value[name]
        if not isinstance(actual, dict) or set(actual) != FROZEN_FILE_KEYS:
            raise OpenStackSourceSetError(
                f"frozen OpenStack source {name} keys must be exactly "
                f"{sorted(FROZEN_FILE_KEYS)!r}"
            )
        if actual != expected:
            raise OpenStackSourceSetError(
                f"frozen OpenStack source {name} does not match its source-set"
            )
    return value


def _nearest_release_from_metadata(
    deliverables_dir: Path, project_name: str
) -> dict[str, str] | None:
    latest: dict[str, str] | None = None
    try:
        paths = sorted(deliverables_dir.glob("*.yaml"))
    except OSError as error:
        raise OpenStackSourceSetError(
            f"cannot enumerate release metadata {deliverables_dir}: {error}"
        ) from error
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as error:
            raise OpenStackSourceSetError(
                f"cannot read release metadata {path}: {error}"
            ) from error
        version: str | None = None
        for index, line in enumerate(lines):
            version_match = re.fullmatch(r"  - version: (\S+)", line)
            if version_match:
                version = version_match.group(1)
                continue
            if line != f"      - repo: {project_name}":
                continue
            if version is None or index + 1 >= len(lines):
                raise OpenStackSourceSetError(
                    f"malformed release metadata for {project_name}: {path}"
                )
            commit_match = re.fullmatch(
                r"        hash: ([0-9a-f]{40})", lines[index + 1]
            )
            if commit_match is None:
                raise OpenStackSourceSetError(
                    f"malformed release hash for {project_name}: {path}"
                )
            latest = {"version": version, "commit": commit_match.group(1)}
    return latest


def validate_source_set_release_metadata(
    source_set_document: dict[str, Any], releases_checkout: Path
) -> None:
    """Prove nearest_release values against a pinned releases checkout."""
    source_set = validate_source_set_document(source_set_document)
    deliverables_dir = (
        releases_checkout / "deliverables" / source_set.document["series"]
    )
    if not deliverables_dir.is_dir():
        raise OpenStackSourceSetError(
            f"release metadata series directory does not exist: {deliverables_dir}"
        )
    for project_name, project in source_set.document["projects"].items():
        if project_name == "openstack/requirements" or not project_name.startswith(
            "openstack/"
        ):
            if project["nearest_release"] is not None:
                raise OpenStackSourceSetError(
                    f"{project_name} nearest_release must be null"
                )
            continue
        expected = _nearest_release_from_metadata(deliverables_dir, project_name)
        if project["nearest_release"] != expected:
            raise OpenStackSourceSetError(
                f"{project_name} nearest_release does not match pinned "
                "OpenStack Releases metadata"
            )


def _profile_images(profile_path: Path, release: str) -> set[str]:
    try:
        profile = json.loads(
            profile_path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise OpenStackSourceSetError(
            f"cannot read deployment profile {profile_path}: {error}"
        ) from error
    if not isinstance(profile, dict) or not isinstance(
        profile.get("build_groups"), list
    ):
        raise OpenStackSourceSetError(
            "deployment profile must contain a build_groups list"
        )
    images: set[str] = set()
    for index, group in enumerate(profile["build_groups"]):
        if not isinstance(group, dict):
            raise OpenStackSourceSetError(
                f"deployment profile build_groups[{index}] must be an object"
            )
        applies_to = group.get("applies_to", {})
        releases = applies_to.get("releases", [release]) if isinstance(
            applies_to, dict
        ) else []
        if release not in releases:
            continue
        parent = group.get("parent")
        parents = group.get("parents", [])
        leaves = group.get("images")
        if (
            not isinstance(parent, str)
            or not isinstance(parents, list)
            or not isinstance(leaves, list)
            or any(not isinstance(value, str) for value in [*parents, *leaves])
        ):
            raise OpenStackSourceSetError(
                f"deployment profile build_groups[{index}] image closure is invalid"
            )
        images.add(parent)
        images.update(parents)
        images.update(leaves)
    if not images:
        raise OpenStackSourceSetError(
            f"deployment profile has no image closure for release {release}"
        )
    return images


def _source_bindings_from_kolla(
    sources: dict[str, dict[str, Any]], images: set[str], release: str
) -> dict[str, dict[str, Any]]:
    bindings: dict[str, dict[str, Any]] = {}
    for section in _selected_source_sections(sources, images):
        source = sources[section]
        project_name = _project_for_kolla_source(source)
        if project_name is None:
            _validate_embedded_archive(section, source)
            continue
        if project_name.startswith("openstack/"):
            repository = f"https://opendev.org/{project_name}"
            track_ref = f"stable/{release}"
        else:
            repository = source["location"].removesuffix(".git").rstrip("/")
            track_ref = source.get("reference")
            if not isinstance(track_ref, str) or not track_ref:
                raise OpenStackSourceSetError(
                    f"external Kolla Git source {section!r} has no track reference"
                )
        existing = bindings.setdefault(
            project_name,
            {
                "repository": repository,
                "track_ref": track_ref,
                "kolla_sections": [],
            },
        )
        if (
            existing["repository"] != repository
            or existing["track_ref"] != track_ref
        ):
            raise OpenStackSourceSetError(
                f"Kolla sources disagree about project {project_name!r}"
            )
        existing["kolla_sections"].append(section)
    for binding in bindings.values():
        binding["kolla_sections"].sort()
    return dict(sorted(bindings.items()))


def generate_source_set_document(
    *,
    source_set_id: str,
    release: str,
    series: str,
    generated_at: str,
    profile_path: Path,
    kolla_source_inputs: Sequence[KollaSourceInput],
    releases_checkout: Path,
    resolve_git_ref: Callable[[str, str], str],
    read_constraints: Callable[[str], bytes],
    read_artifact: Callable[[str], bytes],
) -> dict[str, Any]:
    """Snapshot the complete deployment source closure into schema v3."""
    if not kolla_source_inputs:
        raise OpenStackSourceSetError(
            "at least one pinned Kolla source input is required"
        )
    images = _profile_images(profile_path, release)
    observed: list[dict[str, dict[str, Any]]] = []
    recorded_inputs: dict[str, dict[str, Any]] = {}
    for source_input in kolla_source_inputs:
        if not isinstance(source_input, KollaSourceInput):
            raise OpenStackSourceSetError(
                "Kolla source inputs must be KollaSourceInput values"
            )
        if source_input.version in recorded_inputs:
            raise OpenStackSourceSetError(
                f"duplicate Kolla source input version: {source_input.version!r}"
            )
        try:
            content = source_input.sources_path.read_bytes()
            decoded = content.decode("utf-8")
        except (OSError, UnicodeError) as error:
            raise OpenStackSourceSetError(
                "cannot read pinned Kolla SOURCES for toolchain "
                f"{source_input.version!r}: {source_input.sources_path}: {error}"
            ) from error
        sources = _parse_kolla_sources_content(
            decoded, source_name=str(source_input.sources_path)
        )
        bindings_for_input = _source_bindings_from_kolla(sources, images, release)
        observed.append(bindings_for_input)
        recorded_inputs[source_input.version] = {
            "kolla": {
                "repository": source_input.kolla_repository,
                "commit": source_input.kolla_commit,
                "sources_sha256": hashlib.sha256(content).hexdigest(),
                "closure_sha256": _kolla_source_closure_sha256(
                    bindings_for_input
                ),
            },
            "kolla_ansible": {
                "repository": source_input.kolla_ansible_repository,
                "commit": source_input.kolla_ansible_commit,
            },
        }
    recorded_inputs = dict(sorted(recorded_inputs.items()))
    _validate_kolla_source_inputs(recorded_inputs)
    bindings = observed[0]
    if any(value != bindings for value in observed[1:]):
        raise OpenStackSourceSetError(
            "pinned Kolla toolchains do not share one deployment source contract"
        )
    projects: dict[str, Any] = {}
    deliverables_dir = releases_checkout / "deliverables" / series
    for project_name, binding in bindings.items():
        commit = resolve_git_ref(binding["repository"], binding["track_ref"])
        if not isinstance(commit, str) or not SHA_RE.fullmatch(commit):
            raise OpenStackSourceSetError(
                f"resolved Git ref for {project_name} is not a full commit SHA"
            )
        project = {
            **binding,
            "build_commit": commit,
            "nearest_release": (
                _nearest_release_from_metadata(deliverables_dir, project_name)
                if project_name.startswith("openstack/")
                and project_name != "openstack/requirements"
                else None
            ),
        }
        if project_name == "openstack/requirements":
            constraints = read_constraints(commit)
            if not isinstance(constraints, bytes) or not constraints:
                raise OpenStackSourceSetError(
                    "requirements upper constraints must be non-empty bytes"
                )
            project["upper_constraints_sha256"] = hashlib.sha256(
                constraints
            ).hexdigest()
        projects[project_name] = project
    document = {
        "schema_version": 3,
        "id": source_set_id,
        "release": release,
        "series": series,
        "policy": "stable-head-snapshot",
        "generated_at": generated_at,
        "kolla_source_inputs": recorded_inputs,
        "direct_artifacts": _snapshot_direct_artifacts(
            series,
            resolve_git_ref=resolve_git_ref,
            read_artifact=read_artifact,
        ),
        "projects": projects,
    }
    validate_source_set_document(document)
    validate_source_set_release_metadata(document, releases_checkout)
    return document


def write_new_source_set(path: Path, document: dict[str, Any]) -> None:
    """Create an append-only source-set revision; never replace one in place."""
    source_set = validate_source_set_document(document)
    if path.stem != source_set.document["id"]:
        raise OpenStackSourceSetError(
            f"source-set filename {path.name!r} must match its id"
        )
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as file_obj:
            temporary_path = Path(file_obj.name)
            json.dump(
                source_set.document,
                file_obj,
                ensure_ascii=False,
                indent=2,
            )
            file_obj.write("\n")
            file_obj.flush()
            os.fsync(file_obj.fileno())
        os.link(temporary_path, path)
    except FileExistsError as error:
        raise OpenStackSourceSetError(
            f"source-set already exists and is immutable: {path}"
        ) from error
    except (OSError, UnicodeError) as error:
        raise OpenStackSourceSetError(
            f"cannot write source-set {path}: {error}"
        ) from error
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def _resolve_remote_git_ref(repository: str, track_ref: str) -> str:
    expected_ref = (
        f"refs/heads/{track_ref}"
        if not track_ref.startswith("refs/")
        else track_ref
    )
    result = subprocess.run(
        ["git", "ls-remote", "--refs", repository, expected_ref],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown git error"
        raise OpenStackSourceSetError(
            f"cannot resolve {repository} {expected_ref}: {detail}"
        )
    records = [line.split("\t", 1) for line in result.stdout.splitlines() if line]
    if len(records) != 1 or len(records[0]) != 2 or records[0][1] != expected_ref:
        raise OpenStackSourceSetError(
            f"remote ref must resolve exactly once: {repository} {expected_ref}"
        )
    commit = records[0][0]
    if not SHA_RE.fullmatch(commit):
        raise OpenStackSourceSetError(
            f"remote ref did not resolve to a full commit SHA: {repository}"
        )
    return commit


def _read_remote_constraints(commit: str) -> bytes:
    url = f"https://releases.openstack.org/constraints/upper/{commit}"
    request = Request(url, headers={"User-Agent": "kolla-source-set-generator/1"})
    try:
        with urlopen(request, timeout=30) as response:
            if response.status != 200:
                raise OpenStackSourceSetError(
                    f"constraints request returned HTTP {response.status}: {url}"
                )
            return response.read()
    except (HTTPError, URLError, TimeoutError, OSError) as error:
        raise OpenStackSourceSetError(
            f"cannot read immutable upper constraints {url}: {error}"
        ) from error


def _read_remote_artifact(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": "kolla-source-set-generator/1"})
    try:
        with urlopen(request, timeout=30) as response:
            if response.status != 200:
                raise OpenStackSourceSetError(
                    f"direct artifact request returned HTTP {response.status}: {url}"
                )
            return response.read()
    except (HTTPError, URLError, TimeoutError, OSError) as error:
        raise OpenStackSourceSetError(
            f"cannot read commit-addressed direct artifact {url}: {error}"
        ) from error


def generator_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create an immutable OpenStack stable-branch source-set"
    )
    parser.add_argument("--id", required=True, dest="source_set_id")
    parser.add_argument("--release", required=True)
    parser.add_argument("--series", required=True)
    parser.add_argument("--generated-at", required=True)
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument(
        "--kolla-source-input",
        required=True,
        action="append",
        nargs=6,
        metavar=(
            "VERSION",
            "KOLLA_REPOSITORY",
            "KOLLA_COMMIT",
            "KOLLA_ANSIBLE_REPOSITORY",
            "KOLLA_ANSIBLE_COMMIT",
            "SOURCES_PATH",
        ),
        help=(
            "Exact Kolla/Kolla-Ansible pins and pinned Kolla sources.py; "
            "repeat per compatible toolchain"
        ),
    )
    parser.add_argument("--releases-checkout", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        kolla_source_inputs = [
            KollaSourceInput(
                version=version,
                kolla_repository=kolla_repository,
                kolla_commit=kolla_commit,
                kolla_ansible_repository=kolla_ansible_repository,
                kolla_ansible_commit=kolla_ansible_commit,
                sources_path=Path(sources_path),
            )
            for (
                version,
                kolla_repository,
                kolla_commit,
                kolla_ansible_repository,
                kolla_ansible_commit,
                sources_path,
            ) in args.kolla_source_input
        ]
        document = generate_source_set_document(
            source_set_id=args.source_set_id,
            release=args.release,
            series=args.series,
            generated_at=args.generated_at,
            profile_path=args.profile,
            kolla_source_inputs=kolla_source_inputs,
            releases_checkout=args.releases_checkout,
            resolve_git_ref=_resolve_remote_git_ref,
            read_constraints=_read_remote_constraints,
            read_artifact=_read_remote_artifact,
        )
        write_new_source_set(args.output, document)
        source_set = validate_source_set_document(document)
        print(
            f"Created {args.output}: {source_set.sha256} "
            f"({len(document['projects'])} projects)"
        )
    except OpenStackSourceSetError as error:
        print(f"OpenStack source-set generation failed: {error}", file=sys.stderr)
        return 1
    return 0


def freeze_kolla_sources(
    source_set_document: dict[str, Any],
    kolla_sources_path: Path,
    *,
    images: set[str],
    toolchain_version: str | None = None,
) -> FrozenKollaSources:
    """Render exact source inputs for a Kolla image closure."""
    source_set = validate_source_set_document(source_set_document)
    try:
        content = kolla_sources_path.read_bytes()
        decoded = content.decode("utf-8")
    except (OSError, UnicodeError) as error:
        raise OpenStackSourceSetError(
            f"cannot read pinned Kolla SOURCES from {kolla_sources_path}: {error}"
        ) from error
    if source_set.document["schema_version"] == 3:
        recorded = source_set.document["kolla_source_inputs"].get(
            toolchain_version
        )
        if recorded is None:
            raise OpenStackSourceSetError(
                "active source-set does not record the selected Kolla "
                f"toolchain {toolchain_version!r}"
            )
        actual_sources_sha256 = hashlib.sha256(content).hexdigest()
        if actual_sources_sha256 != recorded["kolla"]["sources_sha256"]:
            raise OpenStackSourceSetError(
                "selected Kolla sources digest does not match the source-set "
                f"toolchain {toolchain_version!r}"
            )
    sources = _parse_kolla_sources_content(
        decoded, source_name=str(kolla_sources_path)
    )
    sections = _selected_source_sections(sources, images)
    _validate_kolla_template_override_seams(
        kolla_sources_path, source_set.document, images
    )
    bindings = _source_section_bindings(source_set.document)
    for bound_section, (project_name, _) in bindings.items():
        kolla_source = sources.get(bound_section)
        if not isinstance(kolla_source, dict):
            raise OpenStackSourceSetError(
                f"source-set binds unknown Kolla source section {bound_section!r}"
            )
        actual_project = _project_for_kolla_source(kolla_source)
        if actual_project != project_name:
            raise OpenStackSourceSetError(
                f"Kolla source section {bound_section!r} maps to "
                f"{actual_project!r}, not source-set project {project_name!r}"
            )
    for section in sections:
        project_name = _project_for_kolla_source(sources[section])
        if project_name is None:
            _validate_embedded_archive(section, sources[section])
            continue
        binding = bindings.get(section)
        if binding is None or binding[0] != project_name:
            raise OpenStackSourceSetError(
                f"source-set is missing Kolla section {section!r} for "
                f"project {project_name!r}"
            )
    return render_frozen_configs(source_set.document)
