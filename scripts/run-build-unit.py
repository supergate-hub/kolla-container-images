#!/usr/bin/env python3
"""Execute one frozen, dependency-aware Kolla build unit."""

from __future__ import annotations

import argparse
import json
import os
import platform as host_platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Sequence

try:
    from scripts.openstack_source_set import validate_frozen_source_contract
except ModuleNotFoundError:
    from openstack_source_set import validate_frozen_source_contract

try:
    from scripts.base_resolution import validate_resolved_base
except ModuleNotFoundError:
    from base_resolution import validate_resolved_base


DIGEST_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
IMAGE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
UNIT_EVIDENCE_SCHEMA_VERSION = 3
UNIT_KEYS = {
    "id",
    "kind",
    "tier",
    "arch",
    "runner",
    "runner_machine",
    "kolla_base_arch",
    "platform",
    "target",
    "ancestor_chain",
    "ancestors",
    "arch_ref",
    "summary_file",
    "logs_dir",
    "command",
}
UNIT_EVIDENCE_KEYS = {
    "schema_version",
    "candidate_id",
    "stream",
    "kolla",
    "base",
    "openstack_sources",
    "unit_id",
    "kind",
    "tier",
    "arch",
    "platform",
    "runner",
    "runner_machine",
    "target",
    "arch_ref",
    "digest",
    "immutable_ref",
    "ancestors",
    "summary",
    "disk_free_bytes",
    "smoke",
}
ANCESTOR_EVIDENCE_KEYS = {"image", "arch_ref", "digest", "immutable_ref"}
SUMMARY_BUCKETS = ("built", "failed", "not_matched", "skipped", "unbuildable")
SUMMARY_ENTRY_KEYS = {
    "built": {"name"},
    "failed": {"name", "status"},
    "not_matched": {"name"},
    "skipped": {"name"},
    "unbuildable": {"name"},
}
FAILED_STATUSES = {"connection_error", "error", "parent_error", "push_error"}

GIB = 1024**3
MIN_PREFLIGHT_FREE_BYTES = 8 * GIB
MIN_BUILD_FREE_BYTES = 2 * GIB
DISK_POLL_INTERVAL_SECONDS = 0.25
DOCKER_ROOT_OVERRIDE = os.environ.get("KOLLA_DOCKER_ROOT")
KOLLA_BUILD_CONFIG_FILE = "artifacts/config/kolla-build.conf"
KOLLA_TEMPLATE_OVERRIDE_FILE = "artifacts/config/template-overrides.j2"
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class BuildUnitError(RuntimeError):
    """A frozen build unit or its evidence is unsafe to execute."""


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


def immutable_ref(arch_ref: str, digest: str) -> str:
    repository, separator, tag = arch_ref.rpartition(":")
    if not separator or not repository or not tag:
        raise BuildUnitError(f"architecture ref is invalid: {arch_ref!r}")
    if not DIGEST_RE.fullmatch(digest):
        raise BuildUnitError(f"digest is invalid: {digest!r}")
    return f"{repository}@{digest}"


def canonical_repository(repository: str) -> str:
    """Expand Docker Hub familiar names without changing repository identity."""
    components = repository.split("/")
    if not repository or any(not component for component in components):
        raise BuildUnitError(f"image repository is invalid: {repository!r}")

    first = components[0]
    has_registry = first == "localhost" or "." in first or ":" in first
    if not has_registry:
        components.insert(0, "docker.io")
    elif first == "index.docker.io":
        components[0] = "docker.io"

    if components[0] == "docker.io" and len(components) == 2:
        components.insert(1, "library")
    return "/".join(components)


def canonical_repo_digest(value: str) -> tuple[str, str]:
    repository, separator, digest = value.rpartition("@")
    if not separator or not repository or not DIGEST_RE.fullmatch(digest):
        raise BuildUnitError(f"immutable image ref is invalid: {value!r}")
    return canonical_repository(repository), digest


def option_value(command: list[str], option: str) -> str:
    positions = [index for index, part in enumerate(command) if part == option]
    if len(positions) != 1 or positions[0] + 1 >= len(command):
        raise BuildUnitError(f"frozen command must contain one value for {option}")
    return command[positions[0] + 1]


def validate_unit(unit: Any) -> dict[str, Any]:
    if type(unit) is not dict or set(unit) != UNIT_KEYS:
        raise BuildUnitError("frozen build unit schema is invalid")
    unit_id = unit["id"]
    target = unit["target"]
    if type(unit_id) is not str or not unit_id:
        raise BuildUnitError("frozen build unit ID is invalid")
    if type(target) is not str or not IMAGE_NAME_RE.fullmatch(target):
        raise BuildUnitError(f"frozen target is invalid: {unit_id}")
    kind = unit["kind"]
    if kind not in {"parent", "leaf"}:
        raise BuildUnitError(f"frozen unit kind is invalid: {unit_id}")
    if type(unit["tier"]) is not int or unit["tier"] < 0:
        raise BuildUnitError(f"frozen unit tier is invalid: {unit_id}")
    if unit_id != f"{unit['arch']}-{kind}-{target}":
        raise BuildUnitError(f"frozen unit ID does not match its identity: {unit_id}")
    expected_arch = {"amd64": ("linux/amd64", "x86_64"), "arm64": ("linux/arm64", "aarch64")}
    if unit["arch"] not in expected_arch:
        raise BuildUnitError(f"frozen unit architecture is invalid: {unit_id}")
    expected_platform, expected_machine = expected_arch[unit["arch"]]
    if unit["platform"] != expected_platform or unit["runner_machine"] != expected_machine:
        raise BuildUnitError(f"frozen unit native platform is invalid: {unit_id}")
    expected_runner = "ubuntu-24.04" if unit["arch"] == "amd64" else "ubuntu-24.04-arm"
    if unit["runner"] != expected_runner:
        raise BuildUnitError(f"frozen unit runner is invalid: {unit_id}")
    expected_base_arch = "x86_64" if unit["arch"] == "amd64" else "aarch64"
    if unit["kolla_base_arch"] != expected_base_arch:
        raise BuildUnitError(f"frozen Kolla base architecture is invalid: {unit_id}")

    ancestor_chain = unit["ancestor_chain"]
    ancestors = unit["ancestors"]
    if (
        type(ancestor_chain) is not list
        or not all(type(name) is str and IMAGE_NAME_RE.fullmatch(name) for name in ancestor_chain)
        or len(ancestor_chain) != len(set(ancestor_chain))
    ):
        raise BuildUnitError(f"frozen ancestor chain is invalid: {unit_id}")
    if type(ancestors) is not list or any(
        type(entry) is not dict
        or set(entry) != {"image", "arch_ref"}
        or type(entry["image"]) is not str
        or type(entry["arch_ref"]) is not str
        for entry in ancestors
    ):
        raise BuildUnitError(f"frozen ancestors are invalid: {unit_id}")
    if [entry["image"] for entry in ancestors] != ancestor_chain:
        raise BuildUnitError(f"frozen ancestors do not match their chain: {unit_id}")
    if kind == "parent" and unit["tier"] != len(ancestor_chain):
        raise BuildUnitError(f"frozen parent tier does not match ancestor depth: {unit_id}")
    if kind == "leaf" and unit["tier"] not in {3, 4}:
        raise BuildUnitError(f"frozen leaf tier must be 3 or 4: {unit_id}")

    for key in ("arch_ref", "summary_file", "logs_dir"):
        if type(unit[key]) is not str or not unit[key]:
            raise BuildUnitError(f"frozen unit {key} is invalid: {unit_id}")
    command = unit["command"]
    if type(command) is not list or not command or not all(type(part) is str for part in command):
        raise BuildUnitError(f"frozen unit command is not structured argv: {unit_id}")
    if command[0] != "kolla-build" or command[-1] != f"^{target}$":
        raise BuildUnitError(f"frozen unit command target is invalid: {unit_id}")
    if command.count("--skip-existing") != 1:
        raise BuildUnitError(
            f"frozen unit command must contain --skip-existing exactly once: {unit_id}"
        )
    if "--skip-parents" in command:
        raise BuildUnitError(
            f"frozen unit command must not contain --skip-parents: {unit_id}"
        )
    if command.count("--push") != 1:
        raise BuildUnitError(f"frozen unit command must contain --push: {unit_id}")
    if option_value(command, "--summary-json-file") != unit["summary_file"]:
        raise BuildUnitError(f"frozen summary path does not match command: {unit_id}")
    if option_value(command, "--logs-dir") != unit["logs_dir"]:
        raise BuildUnitError(f"frozen logs path does not match command: {unit_id}")
    if option_value(command, "--platform") != unit["platform"]:
        raise BuildUnitError(f"frozen command platform is invalid: {unit_id}")
    if option_value(command, "--base-arch") != unit["kolla_base_arch"]:
        raise BuildUnitError(f"frozen command base architecture is invalid: {unit_id}")
    if option_value(command, "--threads") != "1" or option_value(command, "--push-threads") != "1":
        raise BuildUnitError(f"frozen unit thread count must be one: {unit_id}")
    if option_value(command, "--tag") != unit["arch_ref"].rpartition(":")[2]:
        raise BuildUnitError(f"frozen command tag does not match architecture ref: {unit_id}")
    return unit


def all_units(plan: dict[str, Any]) -> list[dict[str, Any]]:
    build = plan.get("build")
    if type(build) is not dict or type(build.get("all_units")) is not list:
        raise BuildUnitError("frozen plan build.all_units must be a list")
    units = [validate_unit(unit) for unit in build["all_units"]]
    unit_ids = [unit["id"] for unit in units]
    if len(unit_ids) != len(set(unit_ids)):
        raise BuildUnitError("frozen plan contains duplicate build unit IDs")
    identities = [(unit["arch"], unit["target"]) for unit in units]
    if len(identities) != len(set(identities)):
        raise BuildUnitError("frozen plan contains duplicate architecture targets")
    return units


def select_unit(plan: dict[str, Any], unit_id: str) -> dict[str, Any]:
    matches = [unit for unit in all_units(plan) if unit["id"] == unit_id]
    if len(matches) != 1:
        raise BuildUnitError(f"frozen plan must contain exactly one unit: {unit_id}")
    return matches[0]


def validate_build_command_contract(
    plan: dict[str, Any], unit: dict[str, Any]
) -> None:
    """Bind executable argv to the frozen source, base, and release inputs."""
    command = unit["command"]
    requested_ref = plan["base"]["requested_ref"]
    base_image, separator, base_tag = requested_ref.rpartition(":")
    if not separator or not base_image or not base_tag:
        raise BuildUnitError("frozen base requested_ref is invalid")
    expected_options = {
        "--engine": "docker",
        "--base": plan.get("distro"),
        "--base-image": base_image,
        "--base-tag": base_tag,
        "--openstack-release": plan.get("release"),
        "--config-file": KOLLA_BUILD_CONFIG_FILE,
        "--locals-base": ".",
        "--registry": plan.get("registry"),
        "--namespace": f"{plan.get('owner')}/{plan.get('repository')}",
    }
    for option, expected in expected_options.items():
        if not isinstance(expected, str) or not expected:
            raise BuildUnitError(
                f"frozen plan cannot determine command option {option}"
            )
        if option_value(command, option) != expected:
            raise BuildUnitError(
                f"frozen command {option} does not match the publish plan"
            )
    if command.count("--nopull") != 1:
        raise BuildUnitError("frozen command must contain --nopull exactly once")

    template_content = plan["openstack_sources"]["template_override"]["content"]
    template_positions = [
        index for index, value in enumerate(command) if value == "--template-override"
    ]
    if template_content:
        if option_value(command, "--template-override") != KOLLA_TEMPLATE_OVERRIDE_FILE:
            raise BuildUnitError(
                "frozen command template override does not match the publish plan"
            )
    elif template_positions:
        raise BuildUnitError(
            "frozen command must not use a template override for an empty plan input"
        )


def validate_plan_identity(plan: Any) -> dict[str, Any]:
    if type(plan) is not dict:
        raise BuildUnitError("frozen publish plan must be an object")
    for key in ("candidate_id", "stream"):
        if type(plan.get(key)) is not str or not plan[key]:
            raise BuildUnitError(f"frozen publish plan {key} is invalid")
    kolla = plan.get("kolla")
    if (
        type(kolla) is not dict
        or set(kolla) != {"repository", "version", "commit"}
        or not all(type(kolla[key]) is str and kolla[key] for key in kolla)
        or not re.fullmatch(r"[0-9a-f]{40}", kolla["commit"])
    ):
        raise BuildUnitError("frozen publish plan Kolla source pin is invalid")
    base = plan.get("base")
    expected_base_keys = {
        "id",
        "requested_ref",
        "index_digest",
        "index_manifest_b64",
        "platforms",
    }
    if (
        type(base) is not dict
        or set(base) != expected_base_keys
        or type(base.get("id")) is not str
        or type(base.get("requested_ref")) is not str
        or not DIGEST_RE.fullmatch(str(base.get("index_digest", "")))
        or type(base.get("platforms")) is not dict
        or set(base["platforms"]) != {"amd64", "arm64"}
    ):
        raise BuildUnitError("frozen publish plan base resolution is invalid")
    for arch, platform in (("amd64", "linux/amd64"), ("arm64", "linux/arm64")):
        descriptor = base["platforms"].get(arch)
        if (
            type(descriptor) is not dict
            or set(descriptor) != {"platform", "digest"}
            or descriptor.get("platform") != platform
            or not DIGEST_RE.fullmatch(str(descriptor.get("digest", "")))
        ):
            raise BuildUnitError("frozen publish plan base platform is invalid")
    base_image, separator, base_tag = base["requested_ref"].rpartition(":")
    if not separator or not base_image or not base_tag:
        raise BuildUnitError("frozen publish plan base resolution is invalid")
    try:
        validate_resolved_base(
            {"id": base["id"], "image": base_image, "tag": base_tag},
            base,
        )
    except ValueError as error:
        raise BuildUnitError(
            "frozen publish plan base resolution is invalid"
        ) from error
    try:
        validate_frozen_source_contract(plan.get("openstack_sources"))
    except ValueError as error:
        raise BuildUnitError(f"frozen OpenStack sources are invalid: {error}") from error
    for unit in all_units(plan):
        validate_build_command_contract(plan, unit)
    return plan


def prepare_frozen_base(
    runner: CommandRunner,
    plan: dict[str, Any],
    unit: dict[str, Any],
) -> None:
    """Seed Kolla's configured base tag from the frozen native descriptor."""
    base = plan["base"]
    repository, separator, tag = base["requested_ref"].rpartition(":")
    if not separator or not repository or not tag:
        raise BuildUnitError("frozen base requested_ref is invalid")
    descriptor = base["platforms"][unit["arch"]]
    immutable_base = f"{repository}@{descriptor['digest']}"
    runner.run(["docker", "pull", "--platform", unit["platform"], immutable_base])
    inspect_local_platform(runner, immutable_base, unit["platform"])
    verify_local_digest(runner, immutable_base, immutable_base)
    runner.run(["docker", "tag", immutable_base, base["requested_ref"]])
    inspect_local_platform(runner, base["requested_ref"], unit["platform"])


def validate_summary(summary: Any, unit: dict[str, Any]) -> dict[str, list[str]]:
    if type(summary) is not dict or set(summary) != set(SUMMARY_BUCKETS):
        raise BuildUnitError("Kolla summary has an invalid top-level schema")
    names_by_bucket: dict[str, list[str]] = {}
    seen: dict[str, str] = {}
    for bucket in SUMMARY_BUCKETS:
        entries = summary[bucket]
        if type(entries) is not list:
            raise BuildUnitError(f"Kolla summary {bucket} must be a list")
        names: list[str] = []
        for index, entry in enumerate(entries):
            if type(entry) is not dict or set(entry) != SUMMARY_ENTRY_KEYS[bucket]:
                raise BuildUnitError(f"Kolla summary {bucket}[{index}] schema is invalid")
            name = entry.get("name")
            if type(name) is not str or not IMAGE_NAME_RE.fullmatch(name):
                raise BuildUnitError(f"Kolla summary {bucket}[{index}] name is invalid")
            if bucket == "failed" and entry.get("status") not in FAILED_STATUSES:
                raise BuildUnitError(f"Kolla summary failed[{index}] status is invalid")
            if name in names or name in seen:
                raise BuildUnitError(f"Kolla summary repeats image {name!r}")
            names.append(name)
            seen[name] = bucket
        names_by_bucket[bucket] = names
    planned_names = set(unit["ancestor_chain"]) | {unit["target"]}
    if names_by_bucket["failed"]:
        raise BuildUnitError("Kolla summary failed bucket must be empty")
    if planned_names & set(names_by_bucket["unbuildable"]):
        raise BuildUnitError("planned images must not appear in Kolla summary unbuildable")
    if planned_names & set(names_by_bucket["not_matched"]):
        raise BuildUnitError("planned images must not appear in Kolla summary not_matched")
    if set(names_by_bucket["built"]) != {unit["target"]} or len(names_by_bucket["built"]) != 1:
        raise BuildUnitError("Kolla summary built set must be exactly the unit target")
    if (
        set(names_by_bucket["skipped"]) != set(unit["ancestor_chain"])
        or len(names_by_bucket["skipped"]) != len(unit["ancestor_chain"])
    ):
        raise BuildUnitError("Kolla summary skipped set must be exactly the ancestor chain")
    return {
        "built": names_by_bucket["built"],
        "skipped": names_by_bucket["skipped"],
    }


class CommandRunner:
    """Small injectable subprocess boundary used by unit tests."""

    def run(self, argv: Sequence[str], *, capture_output: bool = False) -> subprocess.CompletedProcess[str]:
        if not isinstance(argv, (list, tuple)) or not all(isinstance(part, str) for part in argv):
            raise BuildUnitError("command must be structured string argv")
        return subprocess.run(
            list(argv),
            check=True,
            text=True,
            capture_output=capture_output,
            shell=False,
        )

    def run_monitored(
        self,
        argv: Sequence[str],
        disk_sampler: Callable[[], int],
    ) -> int:
        if not isinstance(argv, (list, tuple)) or not all(isinstance(part, str) for part in argv):
            raise BuildUnitError("command must be structured string argv")
        process = subprocess.Popen(list(argv), shell=False)
        minimum = disk_sampler()
        while process.poll() is None:
            minimum = min(minimum, disk_sampler())
            time.sleep(DISK_POLL_INTERVAL_SECONDS)
        return_code = process.wait()
        minimum = min(minimum, disk_sampler())
        if return_code:
            raise subprocess.CalledProcessError(return_code, list(argv))
        return minimum


def docker_root_path(runner: CommandRunner) -> Path:
    if DOCKER_ROOT_OVERRIDE:
        root = Path(DOCKER_ROOT_OVERRIDE)
    else:
        result = runner.run(
            ["docker", "info", "--format", "{{.DockerRootDir}}"],
            capture_output=True,
        )
        root = Path(result.stdout.strip())
    if not root.is_absolute() or not root.is_dir():
        raise BuildUnitError(f"Docker root does not exist: {root}")
    return root


def docker_free_bytes(root: Path) -> int:
    return shutil.disk_usage(root).free


def verify_native_docker_daemon(runner: CommandRunner, unit: dict[str, Any]) -> None:
    result = runner.run(
        ["docker", "info", "--format", "{{.OSType}}/{{.Architecture}}"],
        capture_output=True,
    )
    expected = f"linux/{unit['runner_machine']}"
    if result.stdout.strip() != expected:
        raise BuildUnitError(
            f"Docker daemon must be {expected}, got {result.stdout.strip()!r}"
        )


def inspect_local_platform(
    runner: CommandRunner,
    ref: str,
    expected_platform: str,
) -> None:
    result = runner.run(
        ["docker", "image", "inspect", ref, "--format", "{{.Os}}/{{.Architecture}}"],
        capture_output=True,
    )
    if result.stdout.strip() != expected_platform:
        raise BuildUnitError(
            f"local image {ref} must be {expected_platform}, got {result.stdout.strip()!r}"
        )


def verify_local_ref_absent(runner: CommandRunner, ref: str) -> None:
    result = runner.run(
        ["docker", "image", "ls", "--quiet", "--no-trunc", ref],
        capture_output=True,
    )
    if result.stdout.strip():
        raise BuildUnitError(
            f"target architecture ref already exists locally before build: {ref}"
        )


def verify_local_digest(runner: CommandRunner, ref: str, expected_immutable_ref: str) -> None:
    result = runner.run(
        ["docker", "image", "inspect", ref, "--format", "{{json .RepoDigests}}"],
        capture_output=True,
    )
    try:
        repo_digests = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise BuildUnitError(f"local RepoDigests for {ref} are not JSON") from exc
    if type(repo_digests) is not list:
        raise BuildUnitError(f"local image {ref} does not contain expected digest")
    expected = canonical_repo_digest(expected_immutable_ref)
    for repo_digest in repo_digests:
        if type(repo_digest) is not str:
            continue
        try:
            actual = canonical_repo_digest(repo_digest)
        except BuildUnitError:
            continue
        if actual == expected:
            return
    raise BuildUnitError(f"local image {ref} does not contain expected digest")


def remote_descriptor(
    runner: CommandRunner,
    arch_ref: str,
    expected_platform: str,
) -> tuple[str, str]:
    result = runner.run(
        [
            "docker",
            "buildx",
            "imagetools",
            "inspect",
            arch_ref,
            "--format",
            "{{json .Manifest}}",
        ],
        capture_output=True,
    )
    try:
        descriptor = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise BuildUnitError(f"remote descriptor for {arch_ref} is not JSON") from exc
    if type(descriptor) is not dict:
        raise BuildUnitError(f"remote descriptor for {arch_ref} must be an object")
    if "manifests" in descriptor:
        manifests = descriptor["manifests"]
        if type(manifests) is not list or len(manifests) != 1 or type(manifests[0]) is not dict:
            raise BuildUnitError(f"remote index for {arch_ref} must have one descriptor")
        child = manifests[0]
        child_platform = child.get("platform")
        if type(child_platform) is not dict:
            raise BuildUnitError(f"remote child platform for {arch_ref} is invalid")
        actual_platform = f"{child_platform.get('os')}/{child_platform.get('architecture')}"
        if actual_platform != expected_platform:
            raise BuildUnitError(
                f"remote image {arch_ref} must be {expected_platform}, got {actual_platform}"
            )
        digest = child.get("digest")
    else:
        digest = descriptor.get("digest")
        descriptor_platform = descriptor.get("platform")
        if descriptor_platform is not None:
            if type(descriptor_platform) is not dict:
                raise BuildUnitError(f"remote platform for {arch_ref} is invalid")
            actual_platform = (
                f"{descriptor_platform.get('os')}/{descriptor_platform.get('architecture')}"
            )
            if actual_platform != expected_platform:
                raise BuildUnitError(
                    f"remote image {arch_ref} must be {expected_platform}, got {actual_platform}"
                )
    if type(digest) is not str or not DIGEST_RE.fullmatch(digest):
        raise BuildUnitError(f"remote descriptor for {arch_ref} has no valid digest")
    return digest, immutable_ref(arch_ref, digest)


def validate_input_record(
    record: Any,
    plan: dict[str, Any],
    planned_unit: dict[str, Any],
) -> dict[str, Any]:
    if type(record) is not dict or set(record) != UNIT_EVIDENCE_KEYS:
        raise BuildUnitError("input unit evidence schema is invalid")
    expected = {
        "schema_version": UNIT_EVIDENCE_SCHEMA_VERSION,
        "candidate_id": plan["candidate_id"],
        "stream": plan["stream"],
        "kolla": plan["kolla"],
        "base": plan["base"],
        "openstack_sources": plan["openstack_sources"],
        "unit_id": planned_unit["id"],
        "kind": planned_unit["kind"],
        "tier": planned_unit["tier"],
        "arch": planned_unit["arch"],
        "platform": planned_unit["platform"],
        "runner": planned_unit["runner"],
        "runner_machine": planned_unit["runner_machine"],
        "target": planned_unit["target"],
        "arch_ref": planned_unit["arch_ref"],
    }
    for key, value in expected.items():
        if type(record.get(key)) is not type(value) or record[key] != value:
            raise BuildUnitError(f"input evidence {key} does not match frozen unit")
    digest = record.get("digest")
    if type(digest) is not str or not DIGEST_RE.fullmatch(digest):
        raise BuildUnitError("input evidence digest is invalid")
    if record.get("immutable_ref") != immutable_ref(planned_unit["arch_ref"], digest):
        raise BuildUnitError("input evidence immutable ref is invalid")
    return record


def input_records(directory: Path) -> list[dict[str, Any]]:
    if not directory.exists():
        raise BuildUnitError(f"input evidence directory does not exist: {directory}")
    if not directory.is_dir():
        raise BuildUnitError(f"input evidence path is not a directory: {directory}")
    records: list[dict[str, Any]] = []
    for path in sorted(directory.rglob("*.json")):
        records.append(load_json(path))
    return records


def resolve_ancestors(
    plan: dict[str, Any],
    unit: dict[str, Any],
    evidence_dir: Path,
) -> list[dict[str, Any]]:
    planned_by_identity = {
        (candidate["arch"], candidate["target"]): candidate for candidate in all_units(plan)
    }
    evidence_by_identity: dict[tuple[str, str], dict[str, Any]] = {}
    for record in input_records(evidence_dir):
        if type(record) is not dict:
            raise BuildUnitError("input evidence must contain JSON objects")
        identity = (record.get("arch"), record.get("target"))
        if identity in evidence_by_identity:
            raise BuildUnitError(f"duplicate input evidence for {identity!r}")
        evidence_by_identity[identity] = record

    consumed: list[dict[str, Any]] = []
    for ancestor in unit["ancestors"]:
        identity = (unit["arch"], ancestor["image"])
        planned_ancestor = planned_by_identity.get(identity)
        if planned_ancestor is None or planned_ancestor["tier"] >= unit["tier"]:
            raise BuildUnitError(
                f"ancestor is not one frozen earlier unit: {ancestor['image']}"
            )
        if planned_ancestor["arch_ref"] != ancestor["arch_ref"]:
            raise BuildUnitError(f"ancestor ref does not match frozen parent: {ancestor['image']}")
        record = evidence_by_identity.get(identity)
        if record is None:
            raise BuildUnitError(f"missing input evidence for ancestor: {ancestor['image']}")
        record = validate_input_record(record, plan, planned_ancestor)
        consumed.append(
            {
                "image": ancestor["image"],
                "arch_ref": ancestor["arch_ref"],
                "digest": record["digest"],
                "immutable_ref": record["immutable_ref"],
            }
        )
    return consumed


def execute_build_unit(
    publish_plan: Path,
    unit_id: str,
    input_evidence_dir: Path,
    output: Path,
    *,
    runner: CommandRunner | None = None,
    disk_sampler: Callable[[], int] | None = None,
    machine: str | None = None,
) -> dict[str, Any]:
    if Path.cwd().resolve() != REPOSITORY_ROOT.resolve():
        raise BuildUnitError(
            "build unit must run from the checked-out repository root so frozen "
            "relative source archives cannot be rebound"
        )
    runner = runner or CommandRunner()
    if disk_sampler is None:
        docker_root = docker_root_path(runner)
        disk_sampler = lambda: docker_free_bytes(docker_root)
    plan = validate_plan_identity(load_json(publish_plan))
    unit = select_unit(plan, unit_id)
    actual_machine = machine if machine is not None else host_platform.machine()
    if actual_machine != unit["runner_machine"]:
        raise BuildUnitError(
            f"runner machine must be {unit['runner_machine']}, got {actual_machine}"
        )
    verify_native_docker_daemon(runner, unit)

    initial_free = disk_sampler()
    runner.run(["docker", "system", "prune", "--all", "--force", "--volumes"])
    after_prune = disk_sampler()
    if after_prune < MIN_PREFLIGHT_FREE_BYTES:
        raise BuildUnitError(
            f"Docker preflight free space is below {MIN_PREFLIGHT_FREE_BYTES} bytes"
        )

    consumed_ancestors = resolve_ancestors(plan, unit, input_evidence_dir)
    if unit["target"] == "base":
        prepare_frozen_base(runner, plan, unit)
    for ancestor in consumed_ancestors:
        runner.run(
            [
                "docker",
                "pull",
                "--platform",
                unit["platform"],
                ancestor["immutable_ref"],
            ]
        )
        inspect_local_platform(runner, ancestor["immutable_ref"], unit["platform"])
        verify_local_digest(runner, ancestor["immutable_ref"], ancestor["immutable_ref"])
        runner.run(["docker", "tag", ancestor["immutable_ref"], ancestor["arch_ref"]])
        inspect_local_platform(runner, ancestor["arch_ref"], unit["platform"])
    after_ancestors = disk_sampler()

    summary_path = Path(unit["summary_file"])
    logs_path = Path(unit["logs_dir"])
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    logs_path.mkdir(parents=True, exist_ok=True)
    summary_path.unlink(missing_ok=True)
    verify_local_ref_absent(runner, unit["arch_ref"])
    minimum_during_build = runner.run_monitored(unit["command"], disk_sampler)
    after_build = disk_sampler()
    if min(minimum_during_build, after_build) < MIN_BUILD_FREE_BYTES:
        raise BuildUnitError(
            f"Docker build free space dropped below {MIN_BUILD_FREE_BYTES} bytes"
        )
    summary = validate_summary(load_json(summary_path), unit)

    inspect_local_platform(runner, unit["arch_ref"], unit["platform"])
    digest, target_immutable_ref = remote_descriptor(
        runner, unit["arch_ref"], unit["platform"]
    )
    runner.run(
        ["docker", "pull", "--platform", unit["platform"], target_immutable_ref]
    )
    inspect_local_platform(runner, target_immutable_ref, unit["platform"])
    verify_local_digest(runner, target_immutable_ref, target_immutable_ref)

    smoke: dict[str, Any] | None = None
    if unit["kind"] == "leaf":
        runner.run(
            [
                "docker",
                "run",
                "--rm",
                "--platform",
                unit["platform"],
                "--entrypoint",
                "/bin/true",
                target_immutable_ref,
            ]
        )
        smoke = {
            "platform": unit["platform"],
            "entrypoint": "/bin/true",
            "passed": True,
        }

    evidence = {
        "schema_version": UNIT_EVIDENCE_SCHEMA_VERSION,
        "candidate_id": plan["candidate_id"],
        "stream": plan["stream"],
        "kolla": plan["kolla"],
        "base": plan["base"],
        "openstack_sources": plan["openstack_sources"],
        "unit_id": unit["id"],
        "kind": unit["kind"],
        "tier": unit["tier"],
        "arch": unit["arch"],
        "platform": unit["platform"],
        "runner": unit["runner"],
        "runner_machine": actual_machine,
        "target": unit["target"],
        "arch_ref": unit["arch_ref"],
        "digest": digest,
        "immutable_ref": target_immutable_ref,
        "ancestors": consumed_ancestors,
        "summary": summary,
        "disk_free_bytes": {
            "initial": initial_free,
            "after_prune": after_prune,
            "after_ancestors": after_ancestors,
            "minimum_during_build": minimum_during_build,
            "after_build": after_build,
        },
        "smoke": smoke,
    }
    if set(evidence) != UNIT_EVIDENCE_KEYS:
        raise AssertionError("internal unit evidence schema mismatch")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return evidence


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--publish-plan", required=True, type=Path)
    parser.add_argument("--unit-id", required=True)
    parser.add_argument("--input-evidence-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        evidence = execute_build_unit(
            args.publish_plan,
            args.unit_id,
            args.input_evidence_dir,
            args.output,
        )
    except (
        BuildUnitError,
        OSError,
        ValueError,
        json.JSONDecodeError,
        subprocess.CalledProcessError,
    ) as exc:
        print(f"Build unit failed: {exc}", file=sys.stderr)
        return 1
    print(
        f"Build unit passed: {evidence['unit_id']} -> {evidence['immutable_ref']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
