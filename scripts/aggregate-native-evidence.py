#!/usr/bin/env python3
"""Validate the build-unit closure and aggregate native Kolla evidence."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


DIGEST_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
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
    "kolla_version",
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
ANCESTOR_KEYS = {"image", "arch_ref", "digest", "immutable_ref"}
SUMMARY_KEYS = {"built", "skipped"}
DISK_KEYS = {
    "initial",
    "after_prune",
    "after_ancestors",
    "minimum_during_build",
    "after_build",
}
LEGACY_EVIDENCE_KEYS = {
    "schema_version",
    "stream",
    "arch",
    "platform",
    "runner_machine",
    "kolla_version",
    "parents",
    "images",
}

GIB = 1024**3
MIN_PREFLIGHT_FREE_BYTES = 8 * GIB
MIN_BUILD_FREE_BYTES = 2 * GIB


class EvidenceError(RuntimeError):
    """Unit evidence does not prove the exact frozen build closure."""


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
        raise EvidenceError(f"architecture ref is invalid: {arch_ref!r}")
    if not isinstance(digest, str) or not DIGEST_RE.fullmatch(digest):
        raise EvidenceError(f"digest is invalid: {digest!r}")
    return f"{repository}@{digest}"


def validate_plan(plan: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if type(plan) is not dict:
        raise EvidenceError("frozen publish plan must be an object")
    for key in ("candidate_id", "stream", "kolla_version"):
        if type(plan.get(key)) is not str or not plan[key]:
            raise EvidenceError(f"frozen publish plan {key} is invalid")
    build = plan.get("build")
    if type(build) is not dict:
        raise EvidenceError("frozen publish plan build must be an object")
    all_units = build.get("all_units")
    if type(all_units) is not list:
        raise EvidenceError("frozen publish plan build.all_units must be a list")
    units: list[dict[str, Any]] = []
    for index, unit in enumerate(all_units):
        if type(unit) is not dict or set(unit) != UNIT_KEYS:
            raise EvidenceError(f"frozen build unit {index} schema is invalid")
        unit_id = unit.get("id")
        if type(unit_id) is not str or not unit_id:
            raise EvidenceError(f"frozen build unit {index} ID is invalid")
        if unit.get("kind") not in {"parent", "leaf"}:
            raise EvidenceError(f"frozen build unit kind is invalid: {unit_id}")
        if unit_id != f"{unit.get('arch')}-{unit.get('kind')}-{unit.get('target')}":
            raise EvidenceError(f"frozen build unit identity is invalid: {unit_id}")
        if unit.get("arch") not in {"amd64", "arm64"}:
            raise EvidenceError(f"frozen build unit architecture is invalid: {unit_id}")
        expected_platform = f"linux/{unit['arch']}"
        expected_machine = "x86_64" if unit["arch"] == "amd64" else "aarch64"
        expected_runner = "ubuntu-24.04" if unit["arch"] == "amd64" else "ubuntu-24.04-arm"
        if (
            unit.get("platform") != expected_platform
            or unit.get("runner_machine") != expected_machine
            or unit.get("runner") != expected_runner
        ):
            raise EvidenceError(f"frozen native identity is invalid: {unit_id}")
        chain = unit.get("ancestor_chain")
        ancestors = unit.get("ancestors")
        if (
            type(chain) is not list
            or len(chain) != len(set(chain))
            or not all(type(name) is str and name for name in chain)
            or type(ancestors) is not list
            or any(
                type(entry) is not dict
                or set(entry) != {"image", "arch_ref"}
                or type(entry["image"]) is not str
                or type(entry["arch_ref"]) is not str
                for entry in ancestors
            )
            or [entry["image"] for entry in ancestors] != chain
        ):
            raise EvidenceError(f"frozen ancestor chain is invalid: {unit_id}")
        units.append(unit)
    ids = [unit["id"] for unit in units]
    identities = [(unit["arch"], unit["target"]) for unit in units]
    if len(ids) != len(set(ids)) or len(identities) != len(set(identities)):
        raise EvidenceError("frozen build units are not unique")

    parent_tiers = build.get("parent_tiers")
    leaf_stages = build.get("leaf_stages")
    if type(parent_tiers) is not list or type(leaf_stages) is not list:
        raise EvidenceError("frozen build matrices are invalid")
    matrix_parents: list[dict[str, Any]] = []
    tiers: list[int] = []
    for entry in parent_tiers:
        if type(entry) is not dict or set(entry) != {"tier", "matrix"}:
            raise EvidenceError("frozen parent tier schema is invalid")
        tier = entry["tier"]
        matrix = entry["matrix"]
        if type(tier) is not int or type(matrix) is not dict or set(matrix) != {"include"}:
            raise EvidenceError("frozen parent tier matrix is invalid")
        include = matrix["include"]
        if type(include) is not list:
            raise EvidenceError("frozen parent tier include must be a list")
        if any(type(unit) is not dict or unit.get("tier") != tier for unit in include):
            raise EvidenceError("frozen parent tier contains a mismatched unit")
        tiers.append(tier)
        matrix_parents.extend(include)
    if tiers != sorted(set(tiers)):
        raise EvidenceError("frozen parent tiers must be unique and ordered")
    matrix_leaves: list[dict[str, Any]] = []
    stages: list[int] = []
    for entry in leaf_stages:
        if type(entry) is not dict or set(entry) != {"stage", "matrix"}:
            raise EvidenceError("frozen leaf stage schema is invalid")
        stage = entry["stage"]
        matrix = entry["matrix"]
        if type(stage) is not int or type(matrix) is not dict or set(matrix) != {"include"}:
            raise EvidenceError("frozen leaf stage matrix is invalid")
        include = matrix["include"]
        if type(include) is not list:
            raise EvidenceError("frozen leaf stage include must be a list")
        if any(
            type(unit) is not dict or unit.get("tier") != 3 + stage
            for unit in include
        ):
            raise EvidenceError("frozen leaf stage contains a mismatched unit")
        stages.append(stage)
        matrix_leaves.extend(include)
    if stages != [0, 1]:
        raise EvidenceError("frozen leaf stages must be exactly 0 and 1")
    planned_parents = [unit for unit in units if unit["kind"] == "parent"]
    planned_leaves = [unit for unit in units if unit["kind"] == "leaf"]
    if matrix_parents != planned_parents or matrix_leaves != planned_leaves:
        raise EvidenceError("frozen build matrices do not exactly close over all_units")
    return plan, units


def validate_record(
    record: Any,
    plan: dict[str, Any],
    unit: dict[str, Any],
) -> dict[str, Any]:
    if type(record) is not dict or set(record) != UNIT_EVIDENCE_KEYS:
        raise EvidenceError(f"unit evidence schema is invalid: {unit['id']}")
    expected = {
        "schema_version": 1,
        "candidate_id": plan["candidate_id"],
        "stream": plan["stream"],
        "kolla_version": plan["kolla_version"],
        "unit_id": unit["id"],
        "kind": unit["kind"],
        "tier": unit["tier"],
        "arch": unit["arch"],
        "platform": unit["platform"],
        "runner": unit["runner"],
        "runner_machine": unit["runner_machine"],
        "target": unit["target"],
        "arch_ref": unit["arch_ref"],
    }
    for key, value in expected.items():
        if type(record.get(key)) is not type(value) or record[key] != value:
            raise EvidenceError(f"unit evidence {key} mismatch: {unit['id']}")
    digest = record.get("digest")
    if type(digest) is not str or not DIGEST_RE.fullmatch(digest):
        raise EvidenceError(f"unit evidence digest is invalid: {unit['id']}")
    if record.get("immutable_ref") != immutable_ref(unit["arch_ref"], digest):
        raise EvidenceError(f"unit evidence immutable ref is invalid: {unit['id']}")

    summary = record.get("summary")
    if type(summary) is not dict or set(summary) != SUMMARY_KEYS:
        raise EvidenceError(f"unit summary evidence schema is invalid: {unit['id']}")
    built = summary["built"]
    skipped = summary["skipped"]
    if type(built) is not list or built != [unit["target"]]:
        raise EvidenceError(f"unit built evidence is invalid: {unit['id']}")
    if (
        type(skipped) is not list
        or len(skipped) != len(set(skipped))
        or set(skipped) != set(unit["ancestor_chain"])
    ):
        raise EvidenceError(f"unit skipped evidence is invalid: {unit['id']}")

    disk = record.get("disk_free_bytes")
    if type(disk) is not dict or set(disk) != DISK_KEYS:
        raise EvidenceError(f"unit disk evidence schema is invalid: {unit['id']}")
    if any(type(disk[key]) is not int or disk[key] < 0 for key in DISK_KEYS):
        raise EvidenceError(f"unit disk evidence values are invalid: {unit['id']}")
    if disk["after_prune"] < MIN_PREFLIGHT_FREE_BYTES:
        raise EvidenceError(f"unit preflight disk evidence is too low: {unit['id']}")
    if min(disk["minimum_during_build"], disk["after_build"]) < MIN_BUILD_FREE_BYTES:
        raise EvidenceError(f"unit build disk evidence is too low: {unit['id']}")

    ancestors = record.get("ancestors")
    if type(ancestors) is not list or len(ancestors) != len(unit["ancestors"]):
        raise EvidenceError(f"unit ancestor evidence is incomplete: {unit['id']}")
    for actual, planned in zip(ancestors, unit["ancestors"], strict=True):
        if type(actual) is not dict or set(actual) != ANCESTOR_KEYS:
            raise EvidenceError(f"unit ancestor evidence schema is invalid: {unit['id']}")
        if actual.get("image") != planned["image"] or actual.get("arch_ref") != planned["arch_ref"]:
            raise EvidenceError(f"unit ancestor identity mismatch: {unit['id']}")
        ancestor_digest = actual.get("digest")
        if type(ancestor_digest) is not str or not DIGEST_RE.fullmatch(ancestor_digest):
            raise EvidenceError(f"unit ancestor digest is invalid: {unit['id']}")
        if actual.get("immutable_ref") != immutable_ref(planned["arch_ref"], ancestor_digest):
            raise EvidenceError(f"unit ancestor immutable ref is invalid: {unit['id']}")

    smoke = record.get("smoke")
    if unit["kind"] == "parent":
        if smoke is not None:
            raise EvidenceError(f"parent unit must not claim leaf smoke: {unit['id']}")
    elif (
        type(smoke) is not dict
        or set(smoke) != {"platform", "entrypoint", "passed"}
        or smoke.get("platform") != unit["platform"]
        or smoke.get("entrypoint") != "/bin/true"
        or smoke.get("passed") is not True
    ):
        raise EvidenceError(f"leaf smoke evidence is invalid: {unit['id']}")
    return record


def load_unit_directory(directory: Path) -> list[dict[str, Any]]:
    if not directory.is_dir():
        raise EvidenceError(f"unit evidence directory does not exist: {directory}")
    paths = sorted(directory.rglob("*.json"))
    records: list[dict[str, Any]] = []
    for path in paths:
        record = load_json(path)
        if type(record) is not dict or set(record) != UNIT_EVIDENCE_KEYS:
            raise EvidenceError(f"unexpected JSON in unit evidence directory: {path}")
        records.append(record)
    return records


def exact_record_closure(
    records: list[dict[str, Any]],
    plan: dict[str, Any],
    expected_units: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    raw_by_id: dict[str, dict[str, Any]] = {}
    for record in records:
        unit_id = record.get("unit_id")
        if type(unit_id) is not str or unit_id in raw_by_id:
            raise EvidenceError("unit evidence contains a missing or duplicate unit ID")
        raw_by_id[unit_id] = record
    expected_by_id = {unit["id"]: unit for unit in expected_units}
    if set(raw_by_id) != set(expected_by_id):
        missing = sorted(set(expected_by_id) - set(raw_by_id))
        unexpected = sorted(set(raw_by_id) - set(expected_by_id))
        raise EvidenceError(
            f"unit evidence closure mismatch; missing={missing!r}, unexpected={unexpected!r}"
        )
    return {
        unit["id"]: validate_record(raw_by_id[unit["id"]], plan, unit)
        for unit in expected_units
    }


def validate_dependency_consumers(
    units: list[dict[str, Any]],
    records_by_id: dict[str, dict[str, Any]],
) -> None:
    unit_by_identity = {(unit["arch"], unit["target"]): unit for unit in units}
    for unit in units:
        consumer = records_by_id[unit["id"]]
        for consumed, planned in zip(consumer["ancestors"], unit["ancestors"], strict=True):
            dependency_unit = unit_by_identity.get((unit["arch"], planned["image"]))
            if dependency_unit is None or dependency_unit["tier"] >= unit["tier"]:
                raise EvidenceError(
                    f"consumer references a non-earlier unit: "
                    f"{unit['id']} -> {planned['image']}"
                )
            dependency_record = records_by_id[dependency_unit["id"]]
            if (
                consumed["arch_ref"] != dependency_record["arch_ref"]
                or consumed["digest"] != dependency_record["digest"]
                or consumed["immutable_ref"] != dependency_record["immutable_ref"]
            ):
                raise EvidenceError(
                    f"consumer dependency digest mismatch: "
                    f"{unit['id']} -> {planned['image']}"
                )


def architecture_metadata(plan: dict[str, Any], arch: str) -> dict[str, Any]:
    build = plan["build"]
    architectures = build.get("architectures")
    if type(architectures) is not list:
        raise EvidenceError("frozen legacy architecture metadata must be a list")
    matches = [entry for entry in architectures if type(entry) is dict and entry.get("arch") == arch]
    if len(matches) != 1:
        raise EvidenceError(f"frozen plan must have one legacy architecture entry: {arch}")
    metadata = matches[0]
    for key in ("parents", "images"):
        if type(metadata.get(key)) is not list or any(
            type(entry) is not dict
            or type(entry.get("image")) is not str
            or type(entry.get("arch_ref")) is not str
            for entry in metadata[key]
        ):
            raise EvidenceError(f"frozen legacy {arch} {key} metadata is invalid")
    return metadata


def aggregate_native(
    plan: dict[str, Any],
    units: list[dict[str, Any]],
    unit_evidence_dir: Path,
    output_dir: Path,
) -> list[Path]:
    parent_units = [unit for unit in units if unit["kind"] == "parent"]
    leaf_units = [unit for unit in units if unit["kind"] == "leaf"]
    records_by_id = exact_record_closure(
        load_unit_directory(unit_evidence_dir), plan, units
    )
    validate_dependency_consumers(units, records_by_id)

    output_dir.mkdir(parents=True, exist_ok=True)
    outputs: list[Path] = []
    for arch in ("amd64", "arm64"):
        metadata = architecture_metadata(plan, arch)
        units_by_target = {
            unit["target"]: unit for unit in units if unit["arch"] == arch
        }
        expected_parent_names = [unit["target"] for unit in parent_units if unit["arch"] == arch]
        expected_leaf_names = [unit["target"] for unit in leaf_units if unit["arch"] == arch]
        metadata_parent_names = [entry["image"] for entry in metadata["parents"]]
        metadata_leaf_names = [entry["image"] for entry in metadata["images"]]
        if (
            len(metadata_parent_names) != len(set(metadata_parent_names))
            or set(metadata_parent_names) != set(expected_parent_names)
        ):
            raise EvidenceError(f"legacy parent metadata does not match units: {arch}")
        if (
            len(metadata_leaf_names) != len(set(metadata_leaf_names))
            or not metadata_leaf_names
            or not set(metadata_leaf_names).issubset(expected_leaf_names)
        ):
            raise EvidenceError(f"legacy leaf metadata does not match units: {arch}")
        build_only_leaf_names = set(expected_leaf_names) - set(metadata_leaf_names)
        selected_leaf_ancestors = {
            ancestor
            for name in metadata_leaf_names
            for ancestor in units_by_target[name]["ancestor_chain"]
        }
        if not build_only_leaf_names.issubset(selected_leaf_ancestors):
            raise EvidenceError(
                f"build-only leaf units are not selected leaf dependencies: {arch}"
            )
        parent_output: list[dict[str, Any]] = []
        for planned in metadata["parents"]:
            unit = units_by_target[planned["image"]]
            record = records_by_id[unit["id"]]
            if planned["arch_ref"] != record["arch_ref"]:
                raise EvidenceError(f"legacy parent ref does not match unit: {unit['id']}")
            parent_output.append(
                {
                    "image": unit["target"],
                    "arch_ref": record["arch_ref"],
                    "digest": record["digest"],
                    "immutable_ref": record["immutable_ref"],
                }
            )
        image_output: list[dict[str, Any]] = []
        for planned in metadata["images"]:
            unit = units_by_target[planned["image"]]
            record = records_by_id[unit["id"]]
            if planned["arch_ref"] != record["arch_ref"]:
                raise EvidenceError(f"legacy leaf ref does not match unit: {unit['id']}")
            image_output.append(
                {
                    "image": unit["target"],
                    "arch_ref": record["arch_ref"],
                    "digest": record["digest"],
                    "immutable_ref": record["immutable_ref"],
                    "smoke": record["smoke"],
                }
            )
        machines = {unit["runner_machine"] for unit in units if unit["arch"] == arch}
        if len(machines) != 1:
            raise EvidenceError(f"native runner machine is inconsistent: {arch}")
        evidence = {
            "schema_version": 1,
            "stream": plan["stream"],
            "arch": arch,
            "platform": f"linux/{arch}",
            "runner_machine": machines.pop(),
            "kolla_version": plan["kolla_version"],
            "parents": parent_output,
            "images": image_output,
        }
        if set(evidence) != LEGACY_EVIDENCE_KEYS:
            raise AssertionError("internal legacy evidence schema mismatch")
        output = output_dir / f"native-{arch}.json"
        output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        outputs.append(output)
    return outputs


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--publish-plan", required=True, type=Path)
    parser.add_argument("--unit-evidence-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        plan, units = validate_plan(load_json(args.publish_plan))
        outputs = aggregate_native(
            plan,
            units,
            args.unit_evidence_dir,
            args.output_dir,
        )
    except (EvidenceError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Evidence aggregation failed: {exc}", file=sys.stderr)
        return 1
    print("Evidence aggregation passed: " + ", ".join(str(path) for path in outputs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
