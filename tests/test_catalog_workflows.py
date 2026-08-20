from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FROM_MAIN = ROOT / ".github" / "workflows" / "update-catalog-from-main.yml"
AFTER_PUBLISH = ROOT / ".github" / "workflows" / "update-catalog-after-publish.yml"


class CatalogWorkflowContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.from_main = FROM_MAIN.read_text(encoding="utf-8")
        cls.after_publish = AFTER_PUBLISH.read_text(encoding="utf-8")

    def test_main_workflow_only_reacts_to_catalog_configuration_or_manual_refresh(self) -> None:
        self.assertIn("name: Update catalog from main configuration", self.from_main)
        self.assertIn("push:", self.from_main)
        self.assertIn("branches: [main]", self.from_main)
        self.assertIn("config/build-matrix.json", self.from_main)
        self.assertIn("config/profiles/**", self.from_main)
        self.assertIn("workflow_dispatch:", self.from_main)
        self.assertIn("refresh_mode:", self.from_main)
        for mode in ("incremental", "full"):
            self.assertIn(f"- {mode}", self.from_main)
        self.assertNotIn("deploy-only", self.from_main)
        self.assertNotIn("workflow_run:", self.from_main)

    def test_publish_workflow_only_refreshes_after_a_successful_publish_terminal_artifact(self) -> None:
        self.assertIn("name: Update catalog after image publish", self.after_publish)
        self.assertIn("workflow_run:", self.after_publish)
        self.assertIn('workflows: ["Publish Kolla images"]', self.after_publish)
        self.assertIn("types: [completed]", self.after_publish)
        self.assertIn("github.event.workflow_run.conclusion == 'success'", self.after_publish)
        self.assertIn("select-publish-artifact.py", self.after_publish)
        self.assertIn("validate-publish-summary.py", self.after_publish)
        self.assertIn("--mode publish", self.after_publish)
        self.assertNotIn("workflow_dispatch:", self.after_publish)

    def test_workflows_share_a_serial_catalog_writer(self) -> None:
        for workflow in (self.from_main, self.after_publish):
            with self.subTest(workflow=workflow[:40]):
                self.assertIn("group: catalog-pages", workflow)
                self.assertIn("cancel-in-progress: false", workflow)
                self.assertIn("ref: main", workflow)
                self.assertIn("ref: gh-pages", workflow)
                self.assertIn("catalog.json", workflow)
                self.assertIn("catalog-data.js", workflow)
                self.assertNotIn("git push --force", workflow)
                self.assertIn("contents: write", workflow)
                self.assertIn("packages: read", workflow)
        self.assertNotIn("actions/upload-artifact@", self.from_main)
        self.assertNotIn("actions/download-artifact@", self.from_main)
        self.assertNotIn("actions/configure-pages@", self.from_main)
        self.assertNotIn("actions/upload-pages-artifact@", self.from_main)
        self.assertNotIn("actions/deploy-pages@", self.from_main)
        self.assertIn("git -C pages push origin HEAD:refs/heads/gh-pages", self.from_main)
        self.assertNotIn("actions/upload-artifact@", self.after_publish)
        self.assertNotIn("actions/configure-pages@", self.after_publish)
        self.assertNotIn("actions/upload-pages-artifact@", self.after_publish)
        self.assertNotIn("actions/deploy-pages@", self.after_publish)
        self.assertIn("git -C pages push origin HEAD:refs/heads/gh-pages", self.after_publish)
        self.assertIn("actions: read", self.after_publish)

    def test_workflow_actions_are_pinned_to_full_commit_shas(self) -> None:
        allowed = {
            "actions/checkout",
            "actions/setup-python",
            "actions/download-artifact",
        }
        for workflow in (self.from_main, self.after_publish):
            for action, sha in re.findall(r"uses: ([^@\s]+)@([0-9a-f]{40})", workflow):
                self.assertIn(action, allowed)
                self.assertEqual(len(sha), 40)


if __name__ == "__main__":
    unittest.main()
