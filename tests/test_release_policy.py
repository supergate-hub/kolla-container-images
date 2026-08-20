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
    MAIN_PUBLISH_REF,
    release_branch_for,
    release_for_branch,
    validate_plan_catalog,
    validate_publish_context,
    validate_publish_source,
)


ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = ROOT / "scripts" / "validate-release-context.py"
PLANNER = ROOT / "scripts" / "plan-publish.py"
BASE_INDEX_FIXTURE = ROOT / "tests" / "fixtures" / "oci-base-index.json"


class PublicationPolicyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.matrix = load_matrix()
        cls.stream = cls.matrix["streams"][0]["id"]
        result = subprocess.run(
            [
                sys.executable,
                str(PLANNER),
                "--stream",
                cls.stream,
                "--profile",
                "core",
                "--image",
                "keystone",
                "--candidate-id",
                "1-1",
                "--base-manifest",
                str(BASE_INDEX_FIXTURE),
                "--dry-run",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
        )
        cls.plan = json.loads(result.stdout)

    def test_release_train_labels_remain_canonical_metadata(self) -> None:
        for release, label in {
            "2025.1": "2025-1",
            "2025.2": "2025-2",
            "2026.1": "2026-1",
        }.items():
            with self.subTest(release=release):
                self.assertEqual(release_branch_for(release), label)
                self.assertEqual(release_for_branch(label), release)

    def test_aggregate_catalog_binds_the_entire_frozen_plan(self) -> None:
        self.assertEqual(validate_plan_catalog(self.matrix, self.plan), [])
        mutations = {
            "release": lambda plan: plan.__setitem__("release", "2025.2"),
            "release train": lambda plan: plan.__setitem__(
                "release_branch", "2025-2"
            ),
            "stream": lambda plan: plan.__setitem__("stream", "missing"),
            "Kolla": lambda plan: plan["kolla"].__setitem__("commit", "0" * 40),
            "base": lambda plan: plan["base"].__setitem__(
                "requested_ref", "quay.io/rockylinux/rockylinux:wrong"
            ),
            "source set": lambda plan: plan["openstack_sources"].__setitem__(
                "canonical_digest", "sha256:" + "0" * 64
            ),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                malformed = copy.deepcopy(self.plan)
                mutate(malformed)
                self.assertTrue(validate_plan_catalog(self.matrix, malformed))

    def test_live_publish_requires_protected_main(self) -> None:
        self.assertEqual(
            validate_publish_context(
                self.matrix,
                self.plan,
                MAIN_PUBLISH_REF,
                require_protected=True,
                ref_protected=True,
            ),
            [],
        )
        for git_ref, protected in (
            ("refs/heads/2025-1", True),
            ("refs/heads/feature/test", True),
            ("refs/tags/2025.1", True),
            (MAIN_PUBLISH_REF, False),
        ):
            with self.subTest(git_ref=git_ref, protected=protected):
                self.assertTrue(
                    validate_publish_context(
                        self.matrix,
                        self.plan,
                        git_ref,
                        require_protected=True,
                        ref_protected=protected,
                    )
                )

    def test_cli_fails_closed_without_protected_main(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            matrix_path = temp_path / "matrix.json"
            plan_path = temp_path / "plan.json"
            matrix_path.write_text(json.dumps(self.matrix), encoding="utf-8")
            plan_path.write_text(json.dumps(self.plan), encoding="utf-8")
            command = [
                sys.executable,
                str(VALIDATOR),
                "--matrix",
                str(matrix_path),
                "--publish-plan",
                str(plan_path),
                "--git-ref",
                MAIN_PUBLISH_REF,
                "--require-protected",
                "--ref-protected",
                "true",
            ]
            result = subprocess.run(command, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            rejected = subprocess.run(
                [*command[:-2], "false"], text=True, capture_output=True
            )
            self.assertNotEqual(rejected.returncode, 0)

    def test_every_mutating_workflow_boundary_requires_main(self) -> None:
        self.assertEqual(validate_publish_source(self.plan, MAIN_PUBLISH_REF, True), [])
        for git_ref, protected in (
            ("refs/heads/2025-1", True),
            ("refs/heads/feature/test", True),
            (MAIN_PUBLISH_REF, False),
        ):
            with self.subTest(git_ref=git_ref, protected=protected):
                self.assertTrue(validate_publish_source(self.plan, git_ref, protected))


if __name__ == "__main__":
    unittest.main()
