from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.profile_resolver import load_matrix
from scripts.release_policy import (
    branch_for_ref,
    release_branch_for,
    release_for_branch,
    validate_matrix_branch,
    validate_plan_matrix,
    validate_publish_context,
    validate_publish_source,
)


ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = ROOT / "scripts" / "validate-release-context.py"


class ReleasePolicyTest(unittest.TestCase):
    def branch_matrix(self, release: str) -> dict:
        matrix = load_matrix()
        branch_matrix = copy.deepcopy(matrix)
        matching_streams = [
            stream for stream in matrix["streams"] if stream["release"] == release
        ]
        if matching_streams and release in matrix["toolchains"]:
            branch_matrix["streams"] = copy.deepcopy(matching_streams)
            branch_matrix["toolchains"] = {
                release: copy.deepcopy(matrix["toolchains"][release])
            }
            return branch_matrix

        # Cross-release policy tests must not depend on another release being
        # present in the branch-local production matrix. Retarget the current
        # release's valid shapes while preserving the provenance object schema.
        stream = copy.deepcopy(matrix["streams"][0])
        stream["id"] = f"{release}-synthetic-stream"
        stream["release"] = release
        toolchain = copy.deepcopy(next(iter(matrix["toolchains"].values())))
        toolchain["series"] = f"synthetic-{release}"
        toolchain["release_branch"] = release_branch_for(release)
        branch_matrix["streams"] = [stream]
        branch_matrix["toolchains"] = {
            release: toolchain
        }
        return branch_matrix

    def branch_plan(self, matrix: dict, release: str) -> dict:
        toolchain = matrix["toolchains"][release]
        return {
            "stream": matrix["streams"][0]["id"],
            "release": release,
            "release_series": toolchain["series"],
            "release_branch": release_branch_for(release),
            "release_metadata": copy.deepcopy(matrix["release_metadata"]),
            "kolla": copy.deepcopy(toolchain["kolla"]),
            "kolla_ansible": copy.deepcopy(toolchain["kolla_ansible"]),
        }

    def test_release_and_branch_names_have_one_canonical_mapping(self) -> None:
        cases = {
            "2025.1": "2025-1",
            "2025.2": "2025-2",
            "2026.1": "2026-1",
        }
        for release, branch in cases.items():
            with self.subTest(release=release, branch=branch):
                self.assertEqual(release_branch_for(release), branch)
                self.assertEqual(release_for_branch(branch), release)

    def test_release_and_branch_aliases_are_rejected(self) -> None:
        for release in ("", "2025", "2025-1", "2025.0", "2025.01", True):
            with self.subTest(release=release):
                with self.assertRaises(ValueError):
                    release_branch_for(release)
        for branch in (
            "",
            "2025.1",
            "release/2025-1",
            "2025-0",
            "2025-01",
            True,
        ):
            with self.subTest(branch=branch):
                with self.assertRaises(ValueError):
                    release_for_branch(branch)

        self.assertEqual(branch_for_ref("refs/heads/2025-1"), "2025-1")
        for git_ref in (
            "main",
            "refs/heads/main",
            "refs/tags/2025-1",
            "refs/heads/release/2025-1",
            True,
        ):
            with self.subTest(git_ref=git_ref):
                with self.assertRaises(ValueError):
                    branch_for_ref(git_ref)

    def test_branch_local_matrix_accepts_only_its_release(self) -> None:
        branch_matrix = self.branch_matrix("2025.1")

        self.assertEqual(validate_matrix_branch(branch_matrix, "2025-1"), [])

        foreign_stream = self.branch_matrix("2025.2")["streams"][0]
        branch_matrix["streams"].append(copy.deepcopy(foreign_stream))
        errors = validate_matrix_branch(branch_matrix, "2025-1")
        self.assertTrue(
            any(
                foreign_stream["id"] in error and "2025.2" in error
                for error in errors
            ),
            errors,
        )

    def test_branch_local_matrix_requires_one_matching_toolchain(self) -> None:
        branch_matrix = self.branch_matrix("2025.2")
        branch_matrix["toolchains"] = {}

        errors = validate_matrix_branch(branch_matrix, "2025-2")

        self.assertTrue(any("exactly the '2025.2' toolchain" in error for error in errors))

    def test_branch_matrix_rejects_toolchain_branch_mismatch(self) -> None:
        matrix = self.branch_matrix("2025.1")
        matrix["toolchains"]["2025.1"]["release_branch"] = "2025-2"

        errors = validate_matrix_branch(matrix, "2025-1")

        self.assertTrue(any("release_branch must be '2025-1'" in error for error in errors))

    def test_frozen_plan_is_bound_to_branch_stream_and_source_pins(self) -> None:
        matrix = self.branch_matrix("2025.1")
        plan = self.branch_plan(matrix, "2025.1")
        self.assertEqual(validate_plan_matrix(matrix, plan, "2025-1"), [])
        self.assertEqual(
            validate_publish_context(matrix, plan, "refs/heads/2025-1"),
            [],
        )

        mutations = {
            "release": lambda value: value.__setitem__("release", "2025.2"),
            "release branch": lambda value: value.__setitem__(
                "release_branch", "2025-2"
            ),
            "release series": lambda value: value.__setitem__(
                "release_series", "wrong"
            ),
            "stream": lambda value: value.__setitem__("stream", "missing"),
            "metadata": lambda value: value["release_metadata"].__setitem__(
                "commit", "0" * 40
            ),
            "Kolla": lambda value: value["kolla"].__setitem__(
                "commit", "0" * 40
            ),
            "Kolla-Ansible": lambda value: value["kolla_ansible"].__setitem__(
                "commit", "0" * 40
            ),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                malformed = copy.deepcopy(plan)
                mutate(malformed)
                self.assertTrue(validate_plan_matrix(matrix, malformed, "2025-1"))

    def test_live_publish_context_requires_protected_exact_release_ref(self) -> None:
        matrix = self.branch_matrix("2025.1")
        plan = self.branch_plan(matrix, "2025.1")
        self.assertEqual(
            validate_publish_context(
                matrix,
                plan,
                "refs/heads/2025-1",
                require_protected=True,
                ref_protected=True,
            ),
            [],
        )
        for git_ref, protected in (
            ("refs/heads/main", True),
            ("refs/heads/2025-2", True),
            ("refs/tags/2025-1", True),
            ("refs/heads/2025-1", False),
        ):
            with self.subTest(git_ref=git_ref, protected=protected):
                self.assertTrue(
                    validate_publish_context(
                        matrix,
                        plan,
                        git_ref,
                        require_protected=True,
                        ref_protected=protected,
                    )
                )

    def test_cli_fails_closed_for_matrix_plan_and_protection(self) -> None:
        matrix = self.branch_matrix("2025.1")
        plan = self.branch_plan(matrix, "2025.1")
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            matrix_path = temp_path / "matrix.json"
            plan_path = temp_path / "plan.json"
            matrix_path.write_text(json.dumps(matrix), encoding="utf-8")
            plan_path.write_text(json.dumps(plan), encoding="utf-8")

            matrix_result = subprocess.run(
                [
                    sys.executable,
                    str(VALIDATOR),
                    "matrix",
                    "--matrix",
                    str(matrix_path),
                    "--branch",
                    "2025-1",
                ],
                text=True,
                capture_output=True,
            )
            self.assertEqual(matrix_result.returncode, 0, matrix_result.stderr)

            base_command = [
                sys.executable,
                str(VALIDATOR),
                "publish",
                "--matrix",
                str(matrix_path),
                "--publish-plan",
                str(plan_path),
                "--git-ref",
                "refs/heads/2025-1",
            ]
            self.assertEqual(subprocess.run(base_command).returncode, 0)
            missing_protection = subprocess.run(
                [*base_command, "--require-protected"],
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(missing_protection.returncode, 0)
            protected = subprocess.run(
                [
                    *base_command,
                    "--require-protected",
                    "--ref-protected",
                    "true",
                ],
                text=True,
                capture_output=True,
            )
            self.assertEqual(protected.returncode, 0, protected.stderr)

    def test_publish_source_requires_exact_protected_release_branch(self) -> None:
        plan = {"release": "2025.1"}
        self.assertEqual(
            validate_publish_source(plan, "refs/heads/2025-1", True),
            [],
        )

        for ref, protected in (
            ("refs/heads/main", True),
            ("refs/heads/2025-2", True),
            ("refs/heads/feature/test", True),
            ("refs/tags/2025-1", True),
            ("refs/heads/2025-1", False),
        ):
            with self.subTest(ref=ref, protected=protected):
                self.assertTrue(validate_publish_source(plan, ref, protected))


if __name__ == "__main__":
    unittest.main()
