from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
PLANNER = ROOT / "scripts" / "plan-publish.py"
RUN_BUILD_UNIT = ROOT / "scripts" / "run-build-unit.py"
CANDIDATE_ID = "123456789-1"
TEN_GIB = 10 * 1024**3
THREE_GIB = 3 * 1024**3


def load_module():
    spec = importlib.util.spec_from_file_location("run_build_unit", RUN_BUILD_UNIT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


BUILD_UNIT = load_module()


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


def planned_unit(plan: dict, unit_id: str) -> dict:
    return next(unit for unit in plan["build"]["all_units"] if unit["id"] == unit_id)


def planned_target(plan: dict, arch: str, target: str) -> dict:
    return next(
        unit
        for unit in plan["build"]["all_units"]
        if unit["arch"] == arch and unit["target"] == target
    )


def digest_for(name: str) -> str:
    nibble = format((sum(name.encode("utf-8")) % 15) + 1, "x")
    return f"sha256:{nibble * 64}"


def unit_record(plan: dict, unit: dict) -> dict:
    digest = digest_for(unit["id"])
    repository = unit["arch_ref"].rpartition(":")[0]
    return {
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
        "ancestors": [],
        "summary": {"built": [unit["target"]], "skipped": unit["ancestor_chain"]},
        "disk_free_bytes": {
            "initial": TEN_GIB,
            "after_prune": TEN_GIB,
            "after_ancestors": TEN_GIB,
            "minimum_during_build": THREE_GIB,
            "after_build": THREE_GIB,
        },
        "smoke": None,
    }


class FakeRunner:
    def __init__(
        self,
        unit: dict,
        *,
        bad_summary: bool = False,
        unbuildable: tuple[str, ...] = (),
    ) -> None:
        self.unit = unit
        self.bad_summary = bad_summary
        self.unbuildable = unbuildable
        self.commands: list[list[str]] = []
        self.target_digest = "sha256:" + "f" * 64

    def run(self, argv, *, capture_output=False):
        self.assert_argv(argv)
        command = list(argv)
        self.commands.append(command)
        stdout = ""
        if command[:3] == ["docker", "image", "inspect"]:
            if command[-1] == "{{.Os}}/{{.Architecture}}":
                stdout = self.unit["platform"] + "\n"
            elif command[-1] == "{{json .RepoDigests}}":
                ref = command[3]
                stdout = json.dumps([ref])
        elif command[:4] == ["docker", "buildx", "imagetools", "inspect"]:
            stdout = json.dumps(
                {
                    "digest": self.target_digest,
                    "platform": {
                        "os": "linux",
                        "architecture": self.unit["arch"],
                    },
                }
            )
        elif command[:3] == ["docker", "info", "--format"]:
            stdout = f"linux/{self.unit['runner_machine']}\n"
        return SimpleNamespace(stdout=stdout, returncode=0)

    def run_monitored(self, argv, disk_sampler):
        self.assert_argv(argv)
        self.commands.append(list(argv))
        built = "wrong-image" if self.bad_summary else self.unit["target"]
        summary = {
            "built": [{"name": built}],
            "failed": [],
            "not_matched": [],
            "skipped": [{"name": name} for name in self.unit["ancestor_chain"]],
            "unbuildable": [{"name": name} for name in self.unbuildable],
        }
        path = Path(self.unit["summary_file"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(summary), encoding="utf-8")
        return THREE_GIB

    def assert_argv(self, argv) -> None:
        if type(argv) is not list or not all(type(part) is str for part in argv):
            raise AssertionError(f"command is not structured argv: {argv!r}")


class BuildUnitTest(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = candidate_plan()

    def prepare_unit(self, temp_path: Path, source_plan: dict, unit_id: str):
        plan = copy.deepcopy(source_plan)
        unit = planned_unit(plan, unit_id)
        unit["summary_file"] = str(temp_path / "summary.json")
        unit["logs_dir"] = str(temp_path / "logs")
        summary_position = unit["command"].index("--summary-json-file") + 1
        logs_position = unit["command"].index("--logs-dir") + 1
        unit["command"][summary_position] = unit["summary_file"]
        unit["command"][logs_position] = unit["logs_dir"]
        plan_path = temp_path / "plan.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        evidence_dir = temp_path / "inputs"
        evidence_dir.mkdir()
        ancestor_records = [
            unit_record(plan, planned_target(plan, unit["arch"], name))
            for name in unit["ancestor_chain"]
        ]
        for record in ancestor_records:
            (evidence_dir / f"{record['unit_id']}.json").write_text(
                json.dumps(record), encoding="utf-8"
            )
        return plan, unit, plan_path, evidence_dir

    def prepare_leaf(self, temp_path: Path):
        return self.prepare_unit(
            temp_path,
            self.plan,
            "amd64-leaf-keystone",
        )

    def test_leaf_uses_immutable_ancestors_and_records_native_smoke(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            plan, unit, plan_path, evidence_dir = self.prepare_leaf(temp_path)
            runner = FakeRunner(unit)
            output = temp_path / "unit.json"

            evidence = BUILD_UNIT.execute_build_unit(
                plan_path,
                unit["id"],
                evidence_dir,
                output,
                runner=runner,
                disk_sampler=lambda: TEN_GIB,
                machine="x86_64",
            )

            self.assertEqual(evidence["summary"], {
                "built": ["keystone"],
                "skipped": ["base", "openstack-base", "keystone-base"],
            })
            self.assertEqual(
                [entry["image"] for entry in evidence["ancestors"]],
                unit["ancestor_chain"],
            )
            self.assertEqual(
                evidence["smoke"],
                {"platform": "linux/amd64", "entrypoint": "/bin/true", "passed": True},
            )
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), evidence)
            for ancestor in evidence["ancestors"]:
                self.assertIn(
                    ["docker", "pull", "--platform", "linux/amd64", ancestor["immutable_ref"]],
                    runner.commands,
                )
                self.assertIn(
                    ["docker", "tag", ancestor["immutable_ref"], ancestor["arch_ref"]],
                    runner.commands,
                )
            self.assertIn("--push", unit["command"])
            self.assertFalse(
                any(command[:2] == ["docker", "push"] for command in runner.commands)
            )
            self.assertIn(unit["command"], runner.commands)
            self.assertTrue(all(type(command) is list for command in runner.commands))
            smoke_command = next(
                command for command in runner.commands if command[:2] == ["docker", "run"]
            )
            self.assertEqual(smoke_command[-1], evidence["immutable_ref"])

    def test_summary_must_build_only_target_and_skip_exact_ancestors(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            _, unit, plan_path, evidence_dir = self.prepare_leaf(temp_path)
            output = temp_path / "unit.json"
            with self.assertRaisesRegex(
                BUILD_UNIT.BuildUnitError,
                "built set must be exactly the unit target",
            ):
                BUILD_UNIT.execute_build_unit(
                    plan_path,
                    unit["id"],
                    evidence_dir,
                    output,
                    runner=FakeRunner(unit, bad_summary=True),
                    disk_sampler=lambda: TEN_GIB,
                    machine="x86_64",
                )
            self.assertFalse(output.exists())

    def test_unrelated_unbuildable_catalog_entries_are_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            _, unit, plan_path, evidence_dir = self.prepare_leaf(temp_path)
            output = temp_path / "unit.json"

            evidence = BUILD_UNIT.execute_build_unit(
                plan_path,
                unit["id"],
                evidence_dir,
                output,
                runner=FakeRunner(unit, unbuildable=("collectd", "ovsdpdk")),
                disk_sampler=lambda: TEN_GIB,
                machine="x86_64",
            )

            self.assertEqual(evidence["summary"]["built"], ["keystone"])
            self.assertTrue(output.exists())

    def test_planned_unbuildable_image_is_rejected(self) -> None:
        unit = planned_unit(self.plan, "amd64-leaf-keystone")
        summary = {
            "built": [],
            "failed": [],
            "not_matched": [],
            "skipped": [{"name": name} for name in unit["ancestor_chain"]],
            "unbuildable": [{"name": unit["target"]}],
        }

        with self.assertRaisesRegex(
            BUILD_UNIT.BuildUnitError,
            "planned images must not appear in Kolla summary unbuildable",
        ):
            BUILD_UNIT.validate_summary(summary, unit)

    def test_stale_or_tampered_ancestor_digest_is_rejected_before_build(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            _, unit, plan_path, evidence_dir = self.prepare_leaf(temp_path)
            record_path = sorted(evidence_dir.glob("*.json"))[0]
            record = json.loads(record_path.read_text(encoding="utf-8"))
            record["immutable_ref"] = "ghcr.io/example/wrong@sha256:" + "0" * 64
            record_path.write_text(json.dumps(record), encoding="utf-8")
            runner = FakeRunner(unit)
            with self.assertRaisesRegex(BUILD_UNIT.BuildUnitError, "immutable ref is invalid"):
                BUILD_UNIT.execute_build_unit(
                    plan_path,
                    unit["id"],
                    evidence_dir,
                    temp_path / "unit.json",
                    runner=runner,
                    disk_sampler=lambda: TEN_GIB,
                    machine="x86_64",
                )
            self.assertNotIn(unit["command"], runner.commands)

    def test_stage_one_leaf_uses_stage_zero_leaf_by_immutable_digest(self) -> None:
        deployment_plan = candidate_plan(profile="deployment", image=None)
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            plan, relay, plan_path, evidence_dir = self.prepare_unit(
                temp_path,
                deployment_plan,
                "amd64-leaf-ovn-sb-db-relay",
            )
            server = planned_unit(plan, "amd64-leaf-ovn-sb-db-server")
            self.assertEqual(server["tier"], 3)
            self.assertEqual(relay["tier"], 4)
            self.assertEqual(relay["ancestor_chain"][-1], "ovn-sb-db-server")

            runner = FakeRunner(relay)
            evidence = BUILD_UNIT.execute_build_unit(
                plan_path,
                relay["id"],
                evidence_dir,
                temp_path / "relay-unit.json",
                runner=runner,
                disk_sampler=lambda: TEN_GIB,
                machine="x86_64",
            )

            consumed_server = evidence["ancestors"][-1]
            self.assertEqual(consumed_server["image"], "ovn-sb-db-server")
            self.assertEqual(consumed_server["digest"], digest_for(server["id"]))
            self.assertIn(
                [
                    "docker",
                    "pull",
                    "--platform",
                    "linux/amd64",
                    consumed_server["immutable_ref"],
                ],
                runner.commands,
            )
            self.assertIn(
                [
                    "docker",
                    "tag",
                    consumed_server["immutable_ref"],
                    consumed_server["arch_ref"],
                ],
                runner.commands,
            )

    def test_native_machine_and_disk_gates_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            _, unit, plan_path, evidence_dir = self.prepare_leaf(temp_path)
            with self.assertRaisesRegex(BUILD_UNIT.BuildUnitError, "runner machine must be"):
                BUILD_UNIT.execute_build_unit(
                    plan_path,
                    unit["id"],
                    evidence_dir,
                    temp_path / "wrong-machine.json",
                    runner=FakeRunner(unit),
                    disk_sampler=lambda: TEN_GIB,
                    machine="aarch64",
                )
            low_disk_values = iter((TEN_GIB, BUILD_UNIT.MIN_PREFLIGHT_FREE_BYTES - 1))
            with self.assertRaisesRegex(BUILD_UNIT.BuildUnitError, "preflight free space"):
                BUILD_UNIT.execute_build_unit(
                    plan_path,
                    unit["id"],
                    evidence_dir,
                    temp_path / "low-disk.json",
                    runner=FakeRunner(unit),
                    disk_sampler=lambda: next(low_disk_values),
                    machine="x86_64",
                )


if __name__ == "__main__":
    unittest.main()
