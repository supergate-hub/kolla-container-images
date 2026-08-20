from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "publish-catalog-pages.yml"


class CatalogPagesWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = WORKFLOW.read_text(encoding="utf-8")

    def test_triggers_and_concurrency_keep_catalog_updates_serial(self) -> None:
        self.assertIn("name: Publish image catalog Pages", self.workflow)
        self.assertIn("workflow_run:", self.workflow)
        self.assertIn("workflows: [\"Publish Kolla images\"]", self.workflow)
        self.assertIn("types: [completed]", self.workflow)
        self.assertIn("workflow_dispatch:", self.workflow)
        self.assertIn("schedule:", self.workflow)
        self.assertIn("group: catalog-pages", self.workflow)
        self.assertIn("cancel-in-progress: true", self.workflow)
        self.assertIn(
            "github.event_name != 'workflow_run' || github.event.workflow_run.conclusion == 'success'",
            self.workflow,
        )

    def test_jobs_have_the_minimum_read_write_and_pages_permissions(self) -> None:
        generate = self._job("generate-catalog")
        sync = self._job("sync-gh-pages")
        deploy = self._job("deploy-pages")

        self.assertIn("actions: read", generate)
        self.assertIn("contents: read", generate)
        self.assertIn("packages: read", generate)
        self.assertNotIn("packages: write", generate)
        self.assertIn("contents: write", sync)
        self.assertNotIn("packages: write", sync)
        self.assertIn("pages: write", deploy)
        self.assertIn("id-token: write", deploy)

    def test_catalog_is_generated_from_main_and_synced_to_gh_pages(self) -> None:
        self.assertIn("ref: main", self.workflow)
        self.assertIn("scripts/generate-image-catalog.py", self.workflow)
        self.assertIn("--output _site/catalog.json", self.workflow)
        self.assertIn("CATALOG_PACKAGES_TOKEN", self.workflow)
        self.assertIn("refs/heads/gh-pages", self.workflow)
        self.assertIn("checkout --quiet --orphan gh-pages", self.workflow)
        self.assertIn('commit -m "docs: update published image catalog"', self.workflow)
        self.assertNotIn("git push --force", self.workflow)
        self.assertIn("touch _site/.nojekyll", self.workflow)
        self.assertIn("actions/upload-pages-artifact@", self.workflow)
        self.assertIn("actions/deploy-pages@", self.workflow)

    def test_all_actions_are_pinned_to_full_commit_shas(self) -> None:
        for action, sha, version in re.findall(
            r"uses: ([^@\s]+)@([0-9a-f]{40}) # (v\d+)", self.workflow
        ):
            with self.subTest(action=action):
                self.assertIn(
                    action,
                    {
                        "actions/checkout",
                        "actions/setup-python",
                        "actions/upload-artifact",
                        "actions/download-artifact",
                        "actions/configure-pages",
                        "actions/upload-pages-artifact",
                        "actions/deploy-pages",
                    },
                )
                self.assertEqual(len(sha), 40)
                self.assertRegex(version, r"^v\d+$")

    def _job(self, name: str) -> str:
        expression = rf"(?ms)^  {re.escape(name)}:\n(.*?)(?=^  [a-z][\w-]+:|\Z)"
        match = re.search(expression, self.workflow)
        self.assertIsNotNone(match, f"missing job {name}")
        return match.group(0)
