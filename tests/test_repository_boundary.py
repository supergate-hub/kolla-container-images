from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLAN_PUBLISH = ROOT / "scripts" / "plan-publish.py"
PUBLISH_WORKFLOW = ROOT / ".github" / "workflows" / "publish.yml"
COMPARE_LOCK_SCRIPT = "compare" + "-locks.py"
ENVIRONMENT_ARGUMENT = "--" + "environment"
ENVIRONMENT_LOCK_FIELD = "environment_" + "lock_files"
VALIDATE_LOCK_SCRIPT = "validate" + "-lock.py"


class RepositoryBoundaryTest(unittest.TestCase):
    def render_plan(self) -> dict:
        result = subprocess.run(
            [
                sys.executable,
                str(PLAN_PUBLISH),
                "--profile",
                "deployment",
                "--release",
                "2025.1",
                "--distro",
                "rocky",
                "--distro-version",
                "9",
                "--dry-run",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_publish_plan_has_no_environment_lock_paths(self) -> None:
        rendered_plan = self.render_plan()

        self.assertNotIn(ENVIRONMENT_LOCK_FIELD, rendered_plan)

    def test_publish_workflow_generates_candidate_lock_without_environment_validation(
        self,
    ) -> None:
        self.assertTrue(PUBLISH_WORKFLOW.exists(), "publish workflow is missing")
        workflow = PUBLISH_WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("scripts/generate-lock.py", workflow)
        self.assertNotIn(f"scripts/{VALIDATE_LOCK_SCRIPT}", workflow)
        self.assertNotIn(ENVIRONMENT_ARGUMENT, workflow)

    def test_removed_environment_tools_are_absent(self) -> None:
        self.assertFalse((ROOT / "scripts" / VALIDATE_LOCK_SCRIPT).exists())
        self.assertFalse((ROOT / "scripts" / COMPARE_LOCK_SCRIPT).exists())
        self.assertFalse((ROOT / "locks").exists())


if __name__ == "__main__":
    unittest.main()
