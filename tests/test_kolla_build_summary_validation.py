from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.profile_resolver import load_matrix


ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = ROOT / "scripts" / "validate-kolla-build-summary.py"
PLANNER = ROOT / "scripts" / "plan-publish.py"
CONTRACT = ROOT / "tests" / "fixtures" / "kolla-build-summary-contract.json"
TEST_CANDIDATE_ID = "123456789-1"
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
}


def candidate_plan() -> dict:
    result = subprocess.run(
        [
            sys.executable,
            str(PLANNER),
            "--stream", "2025.1-rocky-9",
            "--profile", "core",
            "--image", "keystone",
            "--candidate-id", TEST_CANDIDATE_ID,
            "--dry-run",
        ],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )
    return json.loads(result.stdout)


def selected_unit(plan: dict, arch: str = "amd64") -> dict:
    return next(
        entry for entry in plan["build"]["all_units"]
        if entry["id"] == f"{arch}-leaf-keystone"
    )


def valid_summary(plan: dict, arch: str = "amd64") -> dict:
    unit = selected_unit(plan, arch)
    return {
        "built": [{"name": unit["target"]}],
        "failed": [],
        "not_matched": [{"name": "glance-api"}],
        "skipped": [{"name": name} for name in unit["ancestor_chain"]],
        "unbuildable": [],
    }


def run_validator(
    plan: dict,
    summary: dict | None = None,
    *,
    arch: str = "amd64",
    raw_summary: str | None = None,
    write_summary: bool = True,
    rewrite_summary_command: bool = True,
) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        plan = copy.deepcopy(plan)
        plan_path = temp_path / "publish-plan.json"
        summary_path = temp_path / "kolla-summary.json"
        command_arch = arch if arch in {"amd64", "arm64"} else "amd64"
        unit_id = f"{arch}-leaf-keystone"
        if rewrite_summary_command:
            unit = selected_unit(plan, command_arch)
            command = unit["command"]
            summary_index = command.index("--summary-json-file") + 1
            command[summary_index] = str(summary_path)
            unit["summary_file"] = str(summary_path)
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        if write_summary:
            content = raw_summary if raw_summary is not None else json.dumps(summary)
            summary_path.write_text(content, encoding="utf-8")
        return subprocess.run(
            [
                sys.executable,
                str(VALIDATOR),
                "--kolla-summary", str(summary_path),
                "--publish-plan", str(plan_path),
                "--unit-id", unit_id,
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )


class KollaBuildSummaryValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = candidate_plan()

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
        matrix_versions = {
            stream["kolla_version"] for stream in load_matrix()["streams"]
        }
        self.assertEqual(set(fixture["versions"]), matrix_versions)
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

    def test_exact_summary_passes(self) -> None:
        result = run_validator(self.plan, valid_summary(self.plan))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Kolla build summary validation passed.", result.stdout)

    def test_exact_arm64_summary_passes(self) -> None:
        result = run_validator(
            self.plan,
            valid_summary(self.plan, "arm64"),
            arch="arm64",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Kolla build summary validation passed.", result.stdout)

    def test_built_must_equal_exact_unit_target(self) -> None:
        missing = valid_summary(self.plan)
        missing["built"].pop()
        extra = valid_summary(self.plan)
        extra["built"].append({"name": "unexpected-image"})
        for name, summary, message in (
            ("missing", missing, "built is missing planned image"),
            ("extra", extra, "built contains unexpected image: unexpected-image"),
        ):
            with self.subTest(case=name):
                result = run_validator(self.plan, summary)
                self.assertEqual(result.returncode, 1)
                self.assertIn(message, result.stderr)

    def test_failures_and_unexpected_skips_are_rejected(self) -> None:
        cases = []
        for status in ("connection_error", "error", "parent_error", "push_error"):
            summary = valid_summary(self.plan)
            summary["failed"] = [{"name": "other-image", "status": status}]
            cases.append((f"failed-{status}", summary, "failed must be empty"))
        unexpected_skip = valid_summary(self.plan)
        unexpected_skip["skipped"].append({"name": "other-image"})
        cases.append(
            (
                "unexpected-skip",
                unexpected_skip,
                "skipped contains unexpected image: other-image",
            )
        )
        for name, summary, message in cases:
            with self.subTest(case=name):
                result = run_validator(self.plan, summary)
                self.assertEqual(result.returncode, 1)
                self.assertIn(message, result.stderr)

    def test_unrelated_unbuildable_catalog_entries_are_allowed(self) -> None:
        summary = valid_summary(self.plan)
        summary["unbuildable"] = [
            {"name": "collectd"},
            {"name": "ovsdpdk"},
        ]
        result = run_validator(self.plan, summary)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_planned_name_must_not_be_unmatched_or_unbuildable(self) -> None:
        unmatched = valid_summary(self.plan)
        unmatched["not_matched"].append({"name": "keystone"})
        unbuildable = valid_summary(self.plan)
        unbuildable["built"] = []
        unbuildable["unbuildable"].append({"name": "keystone"})
        for summary, message in (
            (unmatched, "planned image appears in not_matched: keystone"),
            (unbuildable, "planned image appears in unbuildable: keystone"),
        ):
            with self.subTest(message=message):
                result = run_validator(self.plan, summary)
                self.assertEqual(result.returncode, 1)
                self.assertIn(message, result.stderr)

    def test_duplicate_and_cross_bucket_names_are_rejected(self) -> None:
        duplicate = valid_summary(self.plan)
        duplicate["built"].append(copy.deepcopy(duplicate["built"][0]))
        cross = valid_summary(self.plan)
        cross["not_matched"].append({"name": cross["built"][0]["name"]})
        for summary, message in (
            (duplicate, "built contains duplicate image"),
            (cross, "image appears in both built and not_matched"),
        ):
            result = run_validator(self.plan, summary)
            self.assertEqual(result.returncode, 1)
            self.assertIn(message, result.stderr)

    def test_root_bucket_and_entry_schemas_are_exact(self) -> None:
        cases = []
        missing = valid_summary(self.plan)
        missing.pop("skipped")
        cases.append((missing, "summary keys must be exactly"))
        unexpected = valid_summary(self.plan)
        unexpected["extra"] = []
        cases.append((unexpected, "summary keys must be exactly"))
        not_list = valid_summary(self.plan)
        not_list["skipped"] = {}
        cases.append((not_list, "skipped must be a list"))
        not_object = valid_summary(self.plan)
        not_object["built"][0] = "base"
        cases.append((not_object, "built[0] keys must be exactly"))
        extra_key = valid_summary(self.plan)
        extra_key["built"][0]["status"] = "error"
        cases.append((extra_key, "built[0] keys must be exactly"))
        invalid_name = valid_summary(self.plan)
        invalid_name["built"][0]["name"] = "Bad/Image"
        cases.append((invalid_name, "built[0].name is invalid"))
        invalid_status = valid_summary(self.plan)
        invalid_status["failed"] = [{"name": "other-image", "status": "unknown"}]
        cases.append((invalid_status, "failed[0].status is invalid"))
        for summary, message in cases:
            with self.subTest(message=message):
                result = run_validator(self.plan, summary)
                self.assertEqual(result.returncode, 1)
                self.assertIn(message, result.stderr)

    def test_duplicate_json_key_invalid_json_and_absent_file_exit_two(self) -> None:
        valid = json.dumps(valid_summary(self.plan))
        duplicate = valid.replace('{"built":', '{"built": [], "built":', 1)
        for raw, write_summary, message in (
            (duplicate, True, "duplicate JSON object key"),
            ("{", True, "Expecting"),
            (None, False, "No such file"),
        ):
            with self.subTest(message=message):
                result = run_validator(
                    self.plan,
                    raw_summary=raw,
                    write_summary=write_summary,
                )
                self.assertEqual(result.returncode, 2)
                self.assertIn(message, result.stderr)

    def test_unknown_unit_is_rejected(self) -> None:
        result = run_validator(
            self.plan,
            valid_summary(self.plan),
            arch="ppc64le",
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("exactly one build unit: ppc64le-leaf-keystone", result.stderr)

    def test_malformed_unit_commands_exit_two_without_traceback(self) -> None:
        for command, message in (
            (None, "frozen build unit command must be a string argv list"),
            ([], "frozen build unit command must be a string argv list"),
            ("kolla-build", "frozen build unit command must be a string argv list"),
            ({}, "frozen build unit command must be a string argv list"),
        ):
            with self.subTest(command=command):
                plan = copy.deepcopy(self.plan)
                selected_unit(plan)["command"] = command
                result = run_validator(
                    plan,
                    valid_summary(plan),
                    rewrite_summary_command=False,
                )
                self.assertEqual(result.returncode, 2)
                self.assertIn(message, result.stderr)
                self.assertNotIn("Traceback", result.stderr)

    def test_incomplete_current_summary_rejects_stale_remote_tag_scenario(self) -> None:
        summary = valid_summary(self.plan)
        summary["built"] = [
            entry for entry in summary["built"] if entry["name"] != "keystone"
        ]
        result = run_validator(self.plan, summary)
        self.assertEqual(result.returncode, 1)
        self.assertIn("built is missing planned image: keystone", result.stderr)


if __name__ == "__main__":
    unittest.main()
