from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace

from scripts.profile_resolver import find_stream, load_matrix


ROOT = Path(__file__).resolve().parents[1]
PLANNER = ROOT / "scripts" / "plan-publish.py"
RUN_BUILD_UNIT = ROOT / "scripts" / "run-build-unit.py"
BASE_INDEX_FIXTURE = ROOT / "tests" / "fixtures" / "oci-base-index.json"
CANDIDATE_ID = "123456789-1"
TEN_GIB = 10 * 1024**3
THREE_GIB = 3 * 1024**3
CONTRACT = ROOT / "tests" / "fixtures" / "kolla-build-summary-contract.json"
EXPECTED_METHOD_SHA256 = (
    "02c656c628dc9f127ada22d993e0693fe"
    "ae6c94ee5f42c5d06e9a54fccd959f0"
)
EXPECTED_VERSION_PROVENANCE = {
    "20.4.0": {
        "distribution": "kolla==20.4.0",
        "source_path": "kolla/image/kolla_worker.py",
        "module_sha256": "6a035d50858519474d9b60bf7e502621603c151375ca1bbfc9d06abb7fdf658a",
        "summary_method_sha256": EXPECTED_METHOD_SHA256,
    },
    "20.5.0": {
        "distribution": "kolla==20.5.0",
        "source_path": "kolla/image/kolla_worker.py",
        "module_sha256": "de2428c30f3030c17855103cbc491203d6025fa7427093e41e9cbfe091b6325d",
        "summary_method_sha256": EXPECTED_METHOD_SHA256,
    },
    "21.1.0": {
        "distribution": "kolla==21.1.0",
        "source_path": "kolla/image/kolla_worker.py",
        "module_sha256": "fbaac910754a33c79490d781f9c137953d40ef6ed1624cdd74661970c0d86721",
        "summary_method_sha256": EXPECTED_METHOD_SHA256,
    },
    "22.0.0": {
        "distribution": "kolla==22.0.0",
        "source_path": "kolla/image/kolla_worker.py",
        "module_sha256": "a70c25776f2a10c73aa02fe90a9143fe269af1a1ca39bb2e6f989d737205ef9f",
        "summary_method_sha256": EXPECTED_METHOD_SHA256,
    },
    "22.2.0": {
        "distribution": "kolla==22.2.0",
        "source_path": "kolla/image/kolla_worker.py",
        "module_sha256": "cb377762f5bc5c46af46caa6571170fad2e77754164ae838fe5bdda4e3666ed7",
        "summary_method_sha256": EXPECTED_METHOD_SHA256,
    },
}


def active_stream_id() -> str:
    matrix = json.loads((ROOT / "config" / "build-matrix.json").read_text(
        encoding="utf-8"
    ))
    return matrix["streams"][0]["id"]


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
        active_stream_id(),
        "--profile",
        profile,
        "--candidate-id",
        CANDIDATE_ID,
        "--base-manifest",
        str(BASE_INDEX_FIXTURE),
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
        "schema_version": 3,
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
        docker_hub_familiar_repo_digest: bool = False,
        repo_digest_override: str | None = None,
        remote_inspect_failures: int = 0,
        remote_inspect_error: str = "not found",
        target_present: bool = False,
        unbuildable: tuple[str, ...] = (),
        source_install_failure: bool = False,
    ) -> None:
        self.unit = unit
        self.source_install_failure = source_install_failure
        self.bad_summary = bad_summary
        self.docker_hub_familiar_repo_digest = docker_hub_familiar_repo_digest
        self.repo_digest_override = repo_digest_override
        self.remote_inspect_failures = remote_inspect_failures
        self.remote_inspect_error = remote_inspect_error
        self.target_present = target_present
        self.unbuildable = unbuildable
        self.commands: list[list[str]] = []
        self.remote_inspect_attempts = 0
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
                if self.repo_digest_override is not None:
                    ref = self.repo_digest_override
                elif (
                    self.docker_hub_familiar_repo_digest
                    and ref.startswith("docker.io/library/")
                ):
                    ref = ref.removeprefix("docker.io/library/")
                stdout = json.dumps([ref])
        elif command[:5] == ["docker", "image", "ls", "--quiet", "--no-trunc"]:
            if self.target_present and command[5] == self.unit["arch_ref"]:
                stdout = "sha256:" + "e" * 64 + "\n"
        elif command[:4] == ["docker", "buildx", "imagetools", "inspect"]:
            self.remote_inspect_attempts += 1
            if self.remote_inspect_attempts <= self.remote_inspect_failures:
                raise subprocess.CalledProcessError(
                    1,
                    command,
                    stderr=f"ERROR: {command[4]}: {self.remote_inspect_error}",
                )
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
        elif (
            command[:2] == ["docker", "run"]
            and BUILD_UNIT.SOURCE_INSTALL_CHECK in command
            and self.source_install_failure
        ):
            raise subprocess.CalledProcessError(1, command)
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
    def test_command_runner_retries_only_docker_network_operations(self):
        command = ["docker", "pull", "--platform", "linux/arm64", "registry/image@sha256:" + "a" * 64]
        error = subprocess.CalledProcessError(1, command, stderr="unexpected EOF")
        done = subprocess.CompletedProcess(command, 0, "Downloaded\n", "")
        with patch.object(BUILD_UNIT.subprocess, "run", side_effect=[error, done]) as run, \
             patch("time.sleep") as sleep:
            result = BUILD_UNIT.CommandRunner().run(command, capture_output=True)
        self.assertIs(result, done)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[0], run.call_args_list[1])
        self.assertEqual(run.call_args.kwargs["timeout"], 900)
        sleep.assert_called_once_with(5)
        for command, message in [(["docker", "run", "--rm", "image", "/bin/true"], "connection reset"),
                                 (["docker", "pull", "image"], "unauthorized")]:
            with patch.object(BUILD_UNIT.subprocess, "run", side_effect=subprocess.CalledProcessError(
                    1, command, stderr=message)) as run, patch("time.sleep") as sleep:
                with self.assertRaises(subprocess.CalledProcessError):
                    BUILD_UNIT.CommandRunner().run(command)
            run.assert_called_once()
            sleep.assert_not_called()

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

    def test_completed_unit_is_verified_without_rebuilding(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, unit, plan_path, inputs = self.prepare_unit(root, self.plan, 'amd64-leaf-keystone')
            output = root / 'completed.json'
            first = BUILD_UNIT.execute_build_unit(
                plan_path, unit['id'], inputs, output,
                runner=FakeRunner(unit), disk_sampler=lambda: TEN_GIB, machine='x86_64')
            resumed = FakeRunner(unit)
            result = BUILD_UNIT.execute_build_unit(
                plan_path, unit['id'], inputs, root / 'reused.json',
                runner=resumed, disk_sampler=lambda: TEN_GIB, machine='x86_64',
                reuse_evidence=output)
            self.assertEqual(result, first)
            self.assertFalse(any(cmd[0] == 'kolla-build' for cmd in resumed.commands))
            self.assertFalse(any(cmd[:3] == ['docker', 'system', 'prune'] for cmd in resumed.commands))
            self.assertIn(['docker', 'pull', '--platform', unit['platform'], first['immutable_ref']], resumed.commands)
            self.assertTrue(any(cmd[:2] == ['docker', 'run'] for cmd in resumed.commands))

    def test_completed_checkpoint_requires_matching_identity_and_successful_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, unit, plan_path, inputs = self.prepare_unit(root, self.plan, 'amd64-leaf-keystone')
            output = root / 'completed.json'
            record = BUILD_UNIT.execute_build_unit(
                plan_path, unit['id'], inputs, output,
                runner=FakeRunner(unit), disk_sampler=lambda: TEN_GIB, machine='x86_64')
            replacements = [
                ('candidate_id', '123456789-2'), ('arch', 'arm64'),
                ('kolla', {**record['kolla'], 'commit': '0' * 40}),
                ('summary', {'built': [], 'skipped': unit['ancestor_chain']}),
                ('smoke', {**record['smoke'], 'passed': 1}),
                ('disk_free_bytes', {**record['disk_free_bytes'], 'after_build': 0}),
            ]
            for key, value in replacements:
                with self.subTest(key=key):
                    output.write_text(json.dumps({**record, key: value}))
                    runner = FakeRunner(unit)
                    with self.assertRaises(BUILD_UNIT.BuildUnitError):
                        BUILD_UNIT.execute_build_unit(
                            plan_path, unit['id'], inputs, root / 'reused.json',
                            runner=runner, disk_sampler=lambda: TEN_GIB, machine='x86_64',
                            reuse_evidence=output)
                    self.assertFalse(any(cmd[0] == 'kolla-build' for cmd in runner.commands))
                    self.assertFalse((root / 'reused.json').exists())

    def test_completed_unit_rejects_changed_parent_and_remote_digests(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, unit, plan_path, inputs = self.prepare_unit(root, self.plan, 'amd64-leaf-keystone')
            output = root / 'completed.json'
            record = BUILD_UNIT.execute_build_unit(
                plan_path, unit['id'], inputs, output,
                runner=FakeRunner(unit), disk_sampler=lambda: TEN_GIB, machine='x86_64')
            changed = copy.deepcopy(record)
            changed['ancestors'][0]['digest'] = 'sha256:' + '0' * 64
            output.write_text(json.dumps(changed))
            with self.assertRaisesRegex(BUILD_UNIT.BuildUnitError, 'ancestor'):
                BUILD_UNIT.execute_build_unit(plan_path, unit['id'], inputs, root / 'reused.json',
                    runner=FakeRunner(unit), machine='x86_64', disk_sampler=lambda: TEN_GIB,
                    reuse_evidence=output)
            output.write_text(json.dumps(record))
            runner = FakeRunner(unit)
            runner.target_digest = 'sha256:' + '0' * 64
            with self.assertRaisesRegex(BUILD_UNIT.BuildUnitError, 'digest'):
                BUILD_UNIT.execute_build_unit(plan_path, unit['id'], inputs, root / 'reused.json',
                    runner=runner, machine='x86_64', disk_sampler=lambda: TEN_GIB,
                    reuse_evidence=output)
            self.assertFalse((root / 'reused.json').exists())

    def test_remote_descriptor_retries_a_transient_missing_manifest(self) -> None:
        unit = planned_target(self.plan, "amd64", "keystone")
        runner = FakeRunner(unit, remote_inspect_failures=1)
        waits: list[float] = []

        digest, immutable = BUILD_UNIT.remote_descriptor(
            runner,
            unit["arch_ref"],
            unit["platform"],
            sleep=waits.append,
        )

        self.assertEqual(digest, runner.target_digest)
        self.assertEqual(immutable, f"{unit['arch_ref'].rpartition(':')[0]}@{digest}")
        self.assertEqual(runner.remote_inspect_attempts, 2)
        self.assertEqual(waits, [1])

    def test_remote_descriptor_rejects_a_permanent_registry_error_without_waiting(self) -> None:
        unit = planned_target(self.plan, "amd64", "keystone")
        runner = FakeRunner(
            unit,
            remote_inspect_failures=1,
            remote_inspect_error="denied: requested access to the resource is denied",
        )
        waits: list[float] = []

        with self.assertRaises(subprocess.CalledProcessError):
            BUILD_UNIT.remote_descriptor(
                runner,
                unit["arch_ref"],
                unit["platform"],
                sleep=waits.append,
            )

        self.assertEqual(runner.remote_inspect_attempts, 1)
        self.assertEqual(waits, [])

    def test_remote_descriptor_gives_up_after_bounded_transient_retries(self) -> None:
        unit = planned_target(self.plan, "amd64", "keystone")
        runner = FakeRunner(unit, remote_inspect_failures=6)
        waits: list[float] = []

        with self.assertRaises(subprocess.CalledProcessError):
            BUILD_UNIT.remote_descriptor(
                runner,
                unit["arch_ref"],
                unit["platform"],
                sleep=waits.append,
            )

        self.assertEqual(runner.remote_inspect_attempts, 6)
        self.assertEqual(
            waits,
            list(BUILD_UNIT.REMOTE_DESCRIPTOR_RETRY_DELAYS_SECONDS),
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
            self.assertEqual(evidence["schema_version"], 3)
            self.assertEqual(evidence["kolla"], plan["kolla"])
            self.assertNotIn("kolla_version", evidence)
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
            self.assertIn(
                [
                    "docker", "run", "--rm", "--platform", "linux/amd64",
                    "--entrypoint", "/bin/sh", evidence["immutable_ref"],
                    "-c", BUILD_UNIT.SOURCE_INSTALL_SHELL,
                    "source-install-check", BUILD_UNIT.SOURCE_INSTALL_CHECK,
                ],
                runner.commands,
            )

    def test_leaf_with_a_broken_source_install_writes_no_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            _, unit, plan_path, evidence_dir = self.prepare_leaf(temp_path)
            output = temp_path / "unit.json"
            with self.assertRaises(subprocess.CalledProcessError):
                BUILD_UNIT.execute_build_unit(
                    plan_path,
                    unit["id"],
                    evidence_dir,
                    output,
                    runner=FakeRunner(unit, source_install_failure=True),
                    disk_sampler=lambda: TEN_GIB,
                    machine="x86_64",
                )
            self.assertFalse(output.exists())

            completed = temp_path / "completed.json"
            BUILD_UNIT.execute_build_unit(
                plan_path, unit["id"], evidence_dir, completed,
                runner=FakeRunner(unit), disk_sampler=lambda: TEN_GIB, machine="x86_64")
            # A checkpoint from before the source-install gate cannot be reused
            # once its image fails the gate.
            with self.assertRaises(subprocess.CalledProcessError):
                BUILD_UNIT.execute_build_unit(
                    plan_path, unit["id"], evidence_dir, temp_path / "reused.json",
                    runner=FakeRunner(unit, source_install_failure=True),
                    disk_sampler=lambda: TEN_GIB, machine="x86_64",
                    reuse_evidence=completed)
            self.assertFalse((temp_path / "reused.json").exists())

    def test_source_install_check_flags_missing_console_script_modules(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            site = Path(temp_dir)
            for name, files in (
                ("broken", {"broken/__init__.py": ""}),
                ("healthy", {"healthy/__init__.py": "", "healthy/cmd.py": ""}),
            ):
                for relative, content in files.items():
                    (site / relative).parent.mkdir(parents=True, exist_ok=True)
                    (site / relative).write_text(content, encoding="utf-8")
                dist_info = site / f"{name}-1.0.dist-info"
                dist_info.mkdir()
                (dist_info / "METADATA").write_text(
                    f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n",
                    encoding="utf-8",
                )
                (dist_info / "entry_points.txt").write_text(
                    f"[console_scripts]\n{name}-manage = {name}.cmd:main\n",
                    encoding="utf-8",
                )
                (dist_info / "direct_url.json").write_text(
                    json.dumps({"url": f"file:///{name}", "dir_info": {}}),
                    encoding="utf-8",
                )
            result = subprocess.run(
                [sys.executable, "-S", "-c", BUILD_UNIT.SOURCE_INSTALL_CHECK],
                env={"PYTHONPATH": str(site)},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn(
                "broken: broken-manage -> broken.cmd:main: module not found",
                result.stderr,
            )
            self.assertNotIn("healthy", result.stderr)

    def test_base_unit_pulls_the_frozen_child_digest_before_nopull_build(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            plan, unit, plan_path, evidence_dir = self.prepare_unit(
                temp_path,
                self.plan,
                "amd64-parent-base",
            )
            runner = FakeRunner(unit)

            evidence = BUILD_UNIT.execute_build_unit(
                plan_path,
                unit["id"],
                evidence_dir,
                temp_path / "unit.json",
                runner=runner,
                disk_sampler=lambda: TEN_GIB,
                machine="x86_64",
            )

            base = plan["base"]
            child_digest = base["platforms"]["amd64"]["digest"]
            immutable_base = f"{base['requested_ref'].rsplit(':', 1)[0]}@{child_digest}"
            pull = ["docker", "pull", "--platform", "linux/amd64", immutable_base]
            tag = ["docker", "tag", immutable_base, base["requested_ref"]]
            self.assertLess(runner.commands.index(pull), runner.commands.index(tag))
            self.assertLess(runner.commands.index(tag), runner.commands.index(unit["command"]))
            self.assertIn("--nopull", unit["command"])
            self.assertNotIn("--no-pull", unit["command"])
            self.assertEqual(evidence["base"], base)

    def test_local_digest_accepts_docker_hub_familiar_repo_digest(self) -> None:
        digest = "sha256:" + "a" * 64
        immutable_ref = f"docker.io/library/ubuntu@{digest}"
        unit = planned_unit(self.plan, "amd64-parent-base")
        runner = FakeRunner(unit, docker_hub_familiar_repo_digest=True)

        BUILD_UNIT.verify_local_digest(runner, immutable_ref, immutable_ref)

    def test_local_digest_rejects_same_digest_from_different_repository(self) -> None:
        digest = "sha256:" + "a" * 64
        immutable_ref = f"docker.io/library/ubuntu@{digest}"
        unit = planned_unit(self.plan, "amd64-parent-base")
        runner = FakeRunner(
            unit,
            repo_digest_override=f"docker.io/someone-else/ubuntu@{digest}",
        )

        with self.assertRaisesRegex(
            BUILD_UNIT.BuildUnitError,
            "does not contain expected digest",
        ):
            BUILD_UNIT.verify_local_digest(runner, immutable_ref, immutable_ref)

    def test_target_revision_ref_is_absent_immediately_before_kolla_build(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            _, unit, plan_path, evidence_dir = self.prepare_leaf(temp_path)
            runner = FakeRunner(unit)

            BUILD_UNIT.execute_build_unit(
                plan_path,
                unit["id"],
                evidence_dir,
                temp_path / "unit.json",
                runner=runner,
                disk_sampler=lambda: TEN_GIB,
                machine="x86_64",
            )

            build_index = runner.commands.index(unit["command"])
            self.assertEqual(
                runner.commands[build_index - 1],
                [
                    "docker",
                    "image",
                    "ls",
                    "--quiet",
                    "--no-trunc",
                    unit["arch_ref"],
                ],
            )

    def test_existing_target_revision_ref_is_rejected_before_kolla_build(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            _, unit, plan_path, evidence_dir = self.prepare_leaf(temp_path)
            runner = FakeRunner(unit, target_present=True)

            with self.assertRaisesRegex(
                BUILD_UNIT.BuildUnitError,
                "target architecture ref already exists locally before build",
            ):
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

    def test_build_command_is_bound_to_frozen_base_and_source_inputs(self) -> None:
        plan = copy.deepcopy(self.plan)
        unit = planned_unit(plan, "amd64-leaf-keystone")
        command = unit["command"]
        self.assertEqual(
            command[command.index("--config-file") + 1],
            "artifacts/config/kolla-build.conf",
        )
        self.assertEqual(command.count("--locals-base"), 1)
        self.assertEqual(command[command.index("--locals-base") + 1], ".")
        self.assertEqual(command.count("--nopull"), 1)
        self.assertNotIn("--no-pull", command)
        self.assertEqual(command.count("--skip-existing"), 1)
        self.assertNotIn("--skip-parents", command)

        mutations = {
            "base image": ("--base-image", "example.invalid/moving-base"),
            "base tag": ("--base-tag", "latest"),
            "OpenStack release": ("--openstack-release", "master"),
            "source config": ("--config-file", "/tmp/unfrozen.conf"),
            "source archive base": ("--locals-base", "/tmp/unfrozen"),
        }
        for name, (option, replacement) in mutations.items():
            with self.subTest(name=name):
                malformed = copy.deepcopy(plan)
                malformed_unit = planned_unit(
                    malformed, "amd64-leaf-keystone"
                )
                position = malformed_unit["command"].index(option) + 1
                malformed_unit["command"][position] = replacement
                with self.assertRaisesRegex(
                    BUILD_UNIT.BuildUnitError, "frozen command"
                ):
                    BUILD_UNIT.validate_plan_identity(malformed)

        missing_nopull = copy.deepcopy(plan)
        planned_unit(missing_nopull, "amd64-leaf-keystone")["command"].remove(
            "--nopull"
        )
        with self.assertRaisesRegex(BUILD_UNIT.BuildUnitError, "--nopull"):
            BUILD_UNIT.validate_plan_identity(missing_nopull)

    def test_frozen_command_requires_skip_existing_without_skip_parents(self) -> None:
        unit = copy.deepcopy(planned_unit(self.plan, "amd64-leaf-keystone"))

        BUILD_UNIT.validate_unit(unit)

        for name, mutation in (
            ("missing", lambda command: command.remove("--skip-existing")),
            ("duplicate", lambda command: command.insert(-1, "--skip-existing")),
            ("skip parents", lambda command: command.insert(-1, "--skip-parents")),
        ):
            with self.subTest(name=name):
                malformed = copy.deepcopy(unit)
                mutation(malformed["command"])
                with self.assertRaisesRegex(
                    BUILD_UNIT.BuildUnitError,
                    "--skip-existing" if name != "skip parents" else "--skip-parents",
                ):
                    BUILD_UNIT.validate_unit(malformed)

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

    def test_invalid_or_stale_summary_cannot_accept_existing_remote_image(self) -> None:
        for case in ("missing", "stale", "invalid-json", "duplicate-key", "incomplete"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp_dir:
                temp_path = Path(temp_dir)
                _, unit, plan_path, evidence_dir = self.prepare_leaf(temp_path)
                runner = FakeRunner(unit, target_present=True)
                summary_path = Path(unit["summary_file"])
                if case == "stale":
                    runner.run_monitored(unit["command"], lambda: TEN_GIB)
                valid = {
                    "built": [{"name": unit["target"]}], "failed": [], "not_matched": [],
                    "skipped": [{"name": name} for name in unit["ancestor_chain"]],
                    "unbuildable": [],
                }
                raw = json.dumps(valid)
                raw = {
                    "invalid-json": "{",
                    "duplicate-key": raw.replace('{"built":', '{"built": [], "built":', 1),
                    "incomplete": json.dumps({**valid, "built": []}),
                }.get(case)

                def write_summary(argv, disk_sampler):
                    runner.commands.append(list(argv))
                    self.assertFalse(summary_path.exists(), "stale summary must be removed before build")
                    if raw is not None:
                        summary_path.write_text(raw, encoding="utf-8")
                    return THREE_GIB

                output = temp_path / "unit.json"
                with patch.object(runner, "run_monitored", side_effect=write_summary):
                    with self.assertRaises((BUILD_UNIT.BuildUnitError, ValueError, FileNotFoundError)):
                        BUILD_UNIT.execute_build_unit(
                            plan_path, unit["id"], evidence_dir, output, runner=runner,
                            disk_sampler=lambda: TEN_GIB, machine="x86_64",
                        )
                self.assertFalse(output.exists())
                self.assertEqual(runner.remote_inspect_attempts, 0)

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

    def test_tampered_ancestor_kolla_commit_is_rejected_before_build(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            plan, unit, plan_path, evidence_dir = self.prepare_leaf(temp_path)
            record_path = sorted(evidence_dir.glob("*.json"))[0]
            record = json.loads(record_path.read_text(encoding="utf-8"))
            record["kolla"]["commit"] = "0" * 40
            record_path.write_text(json.dumps(record), encoding="utf-8")
            runner = FakeRunner(unit)

            with self.assertRaisesRegex(
                BUILD_UNIT.BuildUnitError,
                "input evidence kolla does not match frozen unit",
            ):
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


class BuildUnitSummaryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.plan = candidate_plan()

    def valid_summary(self, arch: str = "amd64") -> tuple[dict, dict]:
        unit = planned_unit(self.plan, f"{arch}-leaf-keystone")
        summary = {
            "built": [{"name": unit["target"]}],
            "failed": [],
            "not_matched": [{"name": "glance-api"}],
            "skipped": [{"name": name} for name in unit["ancestor_chain"]],
            "unbuildable": [],
        }
        return unit, summary

    def test_fixture_covers_matrix_pins_and_exact_schema(self) -> None:
        fixture = json.loads(CONTRACT.read_text(encoding="utf-8"))
        self.assertEqual(
            set(fixture),
            {
                "schema_version",
                "source_extraction",
                "summary_method_source",
                "summary_method_sha256",
                "versions",
                "top_level_keys",
                "entry_keys",
                "failed_status_values",
            },
        )
        self.assertEqual(fixture["schema_version"], 1)
        self.assertEqual(
            fixture["source_extraction"],
            "ast.get_source_segment for KollaWorker.summary",
        )
        matrix = load_matrix()
        matrix_versions = {
            find_stream(matrix, stream["id"])["kolla_version"]
            for stream in matrix["streams"]
        }
        self.assertTrue(
            matrix_versions.issubset(fixture["versions"]),
            f"active matrix Kolla versions missing from fixture: "
            f"{sorted(matrix_versions - set(fixture['versions']))!r}",
        )
        self.assertEqual(fixture["versions"], EXPECTED_VERSION_PROVENANCE)
        source_text = fixture["summary_method_source"]
        self.assertIs(type(source_text), str)
        source = source_text.encode("utf-8")
        self.assertFalse(source.endswith(b"\n"))
        self.assertEqual(len(source), 4324)
        self.assertTrue(source_text.startswith("def summary(self):"))
        self.assertTrue(source_text.endswith("        return results"))
        self.assertEqual(hashlib.sha256(source).hexdigest(), EXPECTED_METHOD_SHA256)
        self.assertEqual(fixture["summary_method_sha256"], EXPECTED_METHOD_SHA256)
        self.assertEqual(
            fixture["top_level_keys"],
            ["built", "failed", "not_matched", "skipped", "unbuildable"],
        )
        self.assertEqual(
            fixture["entry_keys"],
            {
                "built": ["name"],
                "failed": ["name", "status"],
                "not_matched": ["name"],
                "skipped": ["name"],
                "unbuildable": ["name"],
            },
        )
        self.assertEqual(
            fixture["failed_status_values"],
            ["connection_error", "error", "parent_error", "push_error"],
        )

        self.assertEqual(fixture["top_level_keys"], list(BUILD_UNIT.SUMMARY_BUCKETS))
        self.assertEqual(
            {key: set(value) for key, value in fixture["entry_keys"].items()},
            BUILD_UNIT.SUMMARY_ENTRY_KEYS,
        )
        self.assertEqual(set(fixture["failed_status_values"]), BUILD_UNIT.FAILED_STATUSES)

    def test_exact_native_summaries_pass(self) -> None:
        for arch in ("amd64", "arm64"):
            with self.subTest(arch=arch):
                unit, summary = self.valid_summary(arch)
                self.assertEqual(BUILD_UNIT.validate_summary(summary, unit), {
                    "built": [unit["target"]], "skipped": unit["ancestor_chain"],
                })

    def test_built_and_skipped_sets_must_match_the_frozen_plan(self) -> None:
        for bucket in ("built", "skipped"):
            for change in ("missing", "extra"):
                with self.subTest(bucket=bucket, change=change):
                    unit, summary = self.valid_summary()
                    if change == "missing":
                        summary[bucket].pop()
                    else:
                        summary[bucket].append({"name": "unexpected-image"})
                    with self.assertRaisesRegex(BUILD_UNIT.BuildUnitError, f"{bucket} set"):
                        BUILD_UNIT.validate_summary(summary, unit)

    def test_all_failure_statuses_reject_the_current_build(self) -> None:
        for status in BUILD_UNIT.FAILED_STATUSES:
            with self.subTest(status=status):
                unit, summary = self.valid_summary()
                summary["failed"] = [{"name": "other-image", "status": status}]
                with self.assertRaisesRegex(BUILD_UNIT.BuildUnitError, "failed bucket must be empty"):
                    BUILD_UNIT.validate_summary(summary, unit)

    def test_planned_targets_and_ancestors_cannot_be_unmatched_or_unbuildable(self) -> None:
        unit, _ = self.valid_summary()
        for name in [unit["target"], *unit["ancestor_chain"]]:
            for bucket in ("not_matched", "unbuildable"):
                with self.subTest(name=name, bucket=bucket):
                    _, summary = self.valid_summary()
                    for entries in summary.values():
                        entries[:] = [entry for entry in entries if entry["name"] != name]
                    summary[bucket].append({"name": name})
                    with self.assertRaisesRegex(BUILD_UNIT.BuildUnitError, f"planned images.*{bucket}"):
                        BUILD_UNIT.validate_summary(summary, unit)

    def test_duplicate_and_cross_bucket_names_are_rejected(self) -> None:
        for bucket in ("built", "not_matched"):
            with self.subTest(bucket=bucket):
                unit, summary = self.valid_summary()
                summary[bucket].append(copy.deepcopy(summary["built"][0]))
                with self.assertRaisesRegex(BUILD_UNIT.BuildUnitError, "repeats image"):
                    BUILD_UNIT.validate_summary(summary, unit)

    def test_root_bucket_and_entry_schemas_are_exact(self) -> None:
        changes = [
            ("missing bucket", lambda s: s.pop("skipped")),
            ("extra bucket", lambda s: s.update(extra=[])),
            ("non-list bucket", lambda s: s.update(skipped={})),
            ("non-object entry", lambda s: s.update(built=["keystone"])),
            ("extra entry field", lambda s: s["built"][0].update(status="error")),
            ("missing entry field", lambda s: s["built"][0].clear()),
            ("invalid name", lambda s: s["built"][0].update(name="Bad/Image")),
            ("non-string name", lambda s: s["built"][0].update(name=[])),
        ]
        for label, change in changes:
            with self.subTest(case=label):
                unit, summary = self.valid_summary()
                change(summary)
                with self.assertRaises(BUILD_UNIT.BuildUnitError):
                    BUILD_UNIT.validate_summary(summary, unit)
        for root in (None, [], "summary"):
            with self.subTest(root=root), self.assertRaises(BUILD_UNIT.BuildUnitError):
                BUILD_UNIT.validate_summary(root, unit)
        for status in ("unknown", None, [], {}, 0):
            with self.subTest(status=status):
                unit, summary = self.valid_summary()
                summary["failed"] = [{"name": "other-image", "status": status}]
                with self.assertRaisesRegex(BUILD_UNIT.BuildUnitError, "status is invalid"):
                    BUILD_UNIT.validate_summary(summary, unit)

    def test_malformed_unit_commands_and_mismatched_summary_path_are_rejected(self) -> None:
        for command in (None, [], "kolla-build", {}):
            with self.subTest(command=command):
                unit = copy.deepcopy(self.valid_summary()[0])
                unit["command"] = command
                with self.assertRaisesRegex(BUILD_UNIT.BuildUnitError, "not structured argv"):
                    BUILD_UNIT.validate_unit(unit)
        unit = copy.deepcopy(self.valid_summary()[0])
        unit["summary_file"] = "another-summary.json"
        with self.assertRaisesRegex(BUILD_UNIT.BuildUnitError, "summary path does not match"):
            BUILD_UNIT.validate_unit(unit)

    def test_unknown_unit_is_rejected(self) -> None:
        with self.assertRaisesRegex(BUILD_UNIT.BuildUnitError, "exactly one.*ppc64le"):
            BUILD_UNIT.select_unit(self.plan, "ppc64le-leaf-keystone")


if __name__ == "__main__":
    unittest.main()
