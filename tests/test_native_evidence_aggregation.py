from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLANNER = ROOT / "scripts" / "plan-publish.py"
AGGREGATOR_PATH = ROOT / "scripts" / "aggregate-native-evidence.py"
CANDIDATE_ID = "123456789-1"
TEN_GIB = 10 * 1024**3
THREE_GIB = 3 * 1024**3


def load_module():
    spec = importlib.util.spec_from_file_location(
        "aggregate_native_evidence", AGGREGATOR_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


AGGREGATOR = load_module()


def candidate_plan(*, profile: str = "core", image: str | None = "keystone") -> dict:
    command = [
        sys.executable,
        str(PLANNER),
        "--stream",
        "2025.1-rocky-9",
        "--profile",
        profile,
        "--candidate-id",
        CANDIDATE_ID,
        "--dry-run",
    ]
    if image is not None:
        command.extend(["--image", image])
    result = subprocess.run(
        command,
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )
    return json.loads(result.stdout)


def digest_for(unit_id: str) -> str:
    return "sha256:" + hashlib.sha256(unit_id.encode("utf-8")).hexdigest()


def records_for(plan: dict) -> dict[str, dict]:
    units = plan["build"]["all_units"]
    unit_by_identity = {(unit["arch"], unit["target"]): unit for unit in units}
    records: dict[str, dict] = {}
    for unit in units:
        digest = digest_for(unit["id"])
        repository = unit["arch_ref"].rpartition(":")[0]
        ancestor_records = []
        for ancestor in unit["ancestors"]:
            parent = unit_by_identity[(unit["arch"], ancestor["image"])]
            parent_digest = digest_for(parent["id"])
            parent_repository = parent["arch_ref"].rpartition(":")[0]
            ancestor_records.append(
                {
                    "image": ancestor["image"],
                    "arch_ref": ancestor["arch_ref"],
                    "digest": parent_digest,
                    "immutable_ref": f"{parent_repository}@{parent_digest}",
                }
            )
        smoke = None
        if unit["kind"] == "leaf":
            smoke = {
                "platform": unit["platform"],
                "entrypoint": "/bin/true",
                "passed": True,
            }
        records[unit["id"]] = {
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
            "digest": digest,
            "immutable_ref": f"{repository}@{digest}",
            "ancestors": ancestor_records,
            "summary": {
                "built": [unit["target"]],
                "skipped": list(reversed(unit["ancestor_chain"])),
            },
            "disk_free_bytes": {
                "initial": TEN_GIB,
                "after_prune": TEN_GIB,
                "after_ancestors": TEN_GIB,
                "minimum_during_build": THREE_GIB,
                "after_build": THREE_GIB,
            },
            "smoke": smoke,
        }
    return records


def write_records(directory: Path, records: list[dict]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for record in records:
        (directory / f"{record['unit_id']}.json").write_text(
            json.dumps(record), encoding="utf-8"
        )


class NativeEvidenceAggregationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = candidate_plan()
        self.plan, self.units = AGGREGATOR.validate_plan(self.plan)
        self.records = records_for(self.plan)

    def prepare_unit_dir(
        self,
        temp_path: Path,
        records: dict[str, dict] | None = None,
    ) -> Path:
        records = records or self.records
        unit_dir = temp_path / "unit-evidence"
        write_records(
            unit_dir,
            [records[unit["id"]] for unit in self.units],
        )
        return unit_dir

    def test_exact_closure_aggregates_legacy_native_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            unit_dir = self.prepare_unit_dir(temp_path)
            native_dir = temp_path / "native"
            outputs = AGGREGATOR.aggregate_native(
                self.plan,
                self.units,
                unit_dir,
                native_dir,
            )
            self.assertEqual(
                outputs,
                [native_dir / "native-amd64.json", native_dir / "native-arm64.json"],
            )
            for arch, output in zip(("amd64", "arm64"), outputs, strict=True):
                evidence = json.loads(output.read_text(encoding="utf-8"))
                self.assertEqual(evidence["arch"], arch)
                self.assertEqual(evidence["platform"], f"linux/{arch}")
                self.assertEqual(evidence["stream"], self.plan["stream"])
                self.assertEqual(evidence["kolla_version"], self.plan["kolla_version"])
                self.assertEqual(
                    set(evidence),
                    {
                        "schema_version",
                        "stream",
                        "arch",
                        "platform",
                        "runner_machine",
                        "kolla_version",
                        "parents",
                        "images",
                    },
                )
                self.assertEqual([image["image"] for image in evidence["images"]], ["keystone"])
                self.assertEqual(
                    evidence["images"][0]["smoke"],
                    {"platform": f"linux/{arch}", "entrypoint": "/bin/true", "passed": True},
                )

    def test_full_deployment_parent_order_aggregates(self) -> None:
        plan, units = AGGREGATOR.validate_plan(
            candidate_plan(profile="deployment", image=None)
        )
        records = records_for(plan)
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            unit_dir = temp_path / "unit-evidence"
            write_records(
                unit_dir,
                [records[unit["id"]] for unit in units],
            )
            outputs = AGGREGATOR.aggregate_native(
                plan, units, unit_dir, temp_path / "native"
            )

            self.assertEqual(len(outputs), 2)
            for output in outputs:
                evidence = json.loads(output.read_text(encoding="utf-8"))
                self.assertEqual(len(evidence["parents"]), 16)
                self.assertEqual(len(evidence["images"]), 63)

            relay = records["amd64-leaf-ovn-sb-db-relay"]
            server = records["amd64-leaf-ovn-sb-db-server"]
            self.assertEqual(relay["tier"], 4)
            self.assertEqual(relay["ancestors"][-1]["image"], "ovn-sb-db-server")
            self.assertEqual(relay["ancestors"][-1]["digest"], server["digest"])

    def test_native_aggregation_rejects_missing_and_unexpected_units(self) -> None:
        all_records = [self.records[unit["id"]] for unit in self.units]
        unexpected = copy.deepcopy(all_records[0])
        unexpected["unit_id"] = "unexpected-unit"
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            for name, records, message in (
                ("missing", all_records[:-1], "closure mismatch"),
                ("unexpected", all_records + [unexpected], "unexpected"),
            ):
                with self.subTest(case=name):
                    directory = temp_path / name
                    write_records(directory, records)
                    with self.assertRaisesRegex(AGGREGATOR.EvidenceError, message):
                        AGGREGATOR.aggregate_native(
                            self.plan, self.units, directory, temp_path / f"out-{name}"
                        )

    def test_native_aggregation_rejects_parent_consumer_digest_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            records = copy.deepcopy(self.records)
            leaf = records["amd64-leaf-keystone"]
            leaf["ancestors"][0]["digest"] = "sha256:" + "0" * 64
            repository = leaf["ancestors"][0]["arch_ref"].rpartition(":")[0]
            leaf["ancestors"][0]["immutable_ref"] = (
                f"{repository}@{leaf['ancestors'][0]['digest']}"
            )
            unit_dir = self.prepare_unit_dir(temp_path, records)
            with self.assertRaisesRegex(AGGREGATOR.EvidenceError, "dependency digest mismatch"):
                AGGREGATOR.aggregate_native(
                    self.plan,
                    self.units,
                    unit_dir,
                    temp_path / "native",
                )

    def test_selected_leaf_dependency_digest_is_fail_closed(self) -> None:
        plan, units = AGGREGATOR.validate_plan(
            candidate_plan(profile="deployment", image=None)
        )
        records = records_for(plan)
        relay = records["amd64-leaf-ovn-sb-db-relay"]
        relay["ancestors"][-1]["digest"] = "sha256:" + "0" * 64
        repository = relay["ancestors"][-1]["arch_ref"].rpartition(":")[0]
        relay["ancestors"][-1]["immutable_ref"] = (
            f"{repository}@{relay['ancestors'][-1]['digest']}"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            unit_dir = temp_path / "unit-evidence"
            write_records(unit_dir, [records[unit["id"]] for unit in units])
            with self.assertRaisesRegex(
                AGGREGATOR.EvidenceError,
                "dependency digest mismatch.*ovn-sb-db-server",
            ):
                AGGREGATOR.aggregate_native(
                    plan, units, unit_dir, temp_path / "native"
                )

    def test_partial_relay_aggregates_build_only_leaf_evidence(self) -> None:
        plan, units = AGGREGATOR.validate_plan(
            candidate_plan(profile="deployment", image="ovn-sb-db-relay")
        )
        records = records_for(plan)
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            unit_dir = temp_path / "unit-evidence"
            write_records(unit_dir, [records[unit["id"]] for unit in units])
            outputs = AGGREGATOR.aggregate_native(
                plan, units, unit_dir, temp_path / "native"
            )

            for output in outputs:
                evidence = json.loads(output.read_text(encoding="utf-8"))
                self.assertEqual(
                    [entry["image"] for entry in evidence["parents"]],
                    ["base", "openvswitch-base", "ovn-base"],
                )
                self.assertEqual(
                    [entry["image"] for entry in evidence["images"]],
                    ["ovn-sb-db-relay"],
                )
            self.assertIn("amd64-leaf-ovn-sb-db-server", records)

    def test_candidate_pin_platform_disk_and_smoke_are_fail_closed(self) -> None:
        cases = (
            ("candidate_id", "wrong-candidate", "candidate_id mismatch"),
            ("kolla_version", "0.0.0", "kolla_version mismatch"),
            ("platform", "linux/arm64", "platform mismatch"),
        )
        for key, value, message in cases:
            with self.subTest(key=key):
                record = copy.deepcopy(self.records["amd64-leaf-keystone"])
                record[key] = value
                unit = next(unit for unit in self.units if unit["id"] == record["unit_id"])
                with self.assertRaisesRegex(AGGREGATOR.EvidenceError, message):
                    AGGREGATOR.validate_record(record, self.plan, unit)

        leaf_unit = next(unit for unit in self.units if unit["id"] == "amd64-leaf-keystone")
        low_disk = copy.deepcopy(self.records[leaf_unit["id"]])
        low_disk["disk_free_bytes"]["minimum_during_build"] = (
            AGGREGATOR.MIN_BUILD_FREE_BYTES - 1
        )
        with self.assertRaisesRegex(AGGREGATOR.EvidenceError, "build disk evidence is too low"):
            AGGREGATOR.validate_record(low_disk, self.plan, leaf_unit)
        failed_smoke = copy.deepcopy(self.records[leaf_unit["id"]])
        failed_smoke["smoke"]["passed"] = False
        with self.assertRaisesRegex(AGGREGATOR.EvidenceError, "leaf smoke evidence is invalid"):
            AGGREGATOR.validate_record(failed_smoke, self.plan, leaf_unit)


if __name__ == "__main__":
    unittest.main()
