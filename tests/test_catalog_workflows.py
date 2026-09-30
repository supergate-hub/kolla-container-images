from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "update-catalog.yml"


def step_block(workflow: str, name: str) -> str:
    start = workflow.index(f"      - name: {name}\n")
    end = workflow.find("\n      - name:", start + 1)
    return workflow[start:end if end != -1 else None]


class CatalogWorkflowContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = WORKFLOW.read_text(encoding="utf-8")

    def test_configuration_manual_and_successful_main_publish_share_one_workflow(self) -> None:
        self.assertIn("push:\n    branches: [main]", self.workflow)
        self.assertIn("config/build-matrix.json", self.workflow)
        self.assertIn("config/profiles/**", self.workflow)
        self.assertIn("workflow_dispatch:", self.workflow)
        self.assertIn("refresh_mode:", self.workflow)
        for mode in ("incremental", "full"):
            self.assertIn(f"- {mode}", self.workflow)
        self.assertIn('workflows: ["Publish Kolla images"]', self.workflow)
        self.assertIn("types: [completed]", self.workflow)
        job_guard = self.workflow.split("    if: >-\n", 1)[1].split("    runs-on:", 1)[0]
        for condition in (
            "github.event_name == 'workflow_run'",
            "github.event.workflow_run.conclusion == 'success'",
            "github.event.workflow_run.event == 'workflow_dispatch'",
            "github.event.workflow_run.head_branch == 'main'",
            "github.event_name == 'push' || github.event_name == 'workflow_dispatch'",
            "github.ref == 'refs/heads/main'",
        ):
            self.assertIn(condition, job_guard)

    def test_publish_artifact_is_bound_to_triggering_run_and_attempt(self) -> None:
        for name in ("List triggering workflow artifacts", "Select terminal publish artifact"):
            block = step_block(self.workflow, name)
            self.assertIn("if: ${{ github.event_name == 'workflow_run' }}", block)
            self.assertIn("github.event.workflow_run.id", block)
        selection = step_block(self.workflow, "Select terminal publish artifact")
        self.assertIn("select-publish-artifact.py", selection)
        self.assertIn('github.event.workflow_run.run_attempt', selection)
        self.assertIn('--run-attempt "$PUBLISH_RUN_ATTEMPT"', selection)
        download = step_block(self.workflow, "Download terminal publish artifact")
        self.assertIn("name: ${{ steps.artifact.outputs.artifact_name }}", download)
        self.assertIn("run-id: ${{ github.event.workflow_run.id }}", download)

    def test_publish_requires_validated_summary_and_plan_cannot_mutate_catalog(self) -> None:
        for name in ("Download terminal publish artifact", "Validate terminal publish summary"):
            self.assertIn("if: ${{ steps.artifact.outputs.should_refresh == 'true' }}",
                          step_block(self.workflow, name))
        summary = step_block(self.workflow, "Validate terminal publish summary")
        self.assertIn('"${#summaries[@]}" -ne 1', summary)
        self.assertIn("scripts/validate-publish-summary.py", summary)
        self.assertIn('["--allow-partial", "--image", scope["image"]]', summary)
        self.assertLess(summary.index("subprocess.run(command, check=True)"),
                        summary.index('"$GITHUB_OUTPUT"'))
        render = step_block(self.workflow, "Render and commit changed catalog data")
        self.assertIn("if: ${{ github.event_name != 'workflow_run' || "
                      "steps.artifact.outputs.should_refresh == 'true' }}", render)
        self.assertIn("PUBLISH_SUMMARY: ${{ steps.summary.outputs.path }}", render)
        self.assertLess(self.workflow.index("- name: Validate terminal publish summary"),
                        self.workflow.index("- name: Render and commit changed catalog data"))
        noop = step_block(self.workflow, "Mark successful plan run as a no-op")
        self.assertIn("github.event_name == 'workflow_run' && "
                      "steps.artifact.outputs.should_refresh != 'true'", noop)

    def test_catalog_writer_preserves_existing_pages_publication(self) -> None:
        for value in ("group: catalog-pages", "cancel-in-progress: false", "ref: main",
                      "ref: gh-pages", "contents: write", "packages: read", "actions: read"):
            self.assertIn(value, self.workflow)
        for action in ("configure-pages", "upload-pages-artifact", "deploy-pages"):
            self.assertNotIn(f"actions/{action}@", self.workflow)
        self.assertNotIn("--force", self.workflow)
        self.assertIn("git -C pages push origin HEAD:refs/heads/gh-pages", self.workflow)
        self.assertEqual(self.workflow.count("scripts/generate-image-catalog.py"), 1)

    def test_every_workflow_action_is_pinned_to_a_full_commit_sha(self) -> None:
        actions = re.findall(r"uses: (\S+)", self.workflow)
        self.assertEqual(len(actions), 4)
        for action in actions:
            self.assertRegex(action, r"^actions/(checkout|setup-python|download-artifact)@[0-9a-f]{40}$")

    def run_render(self, event: str, mode: str = "full", *, changed: bool = False):
        block = step_block(self.workflow, "Render and commit changed catalog data")
        script = textwrap.dedent(block.split("        run: |\n", 1)[1])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "bin"
            binary.mkdir()
            (root / "pages").mkdir()
            for filename, content in (("catalog.json", "{}\n"), ("catalog-data.js", "window.IMAGE_CATALOG = {};\n")):
                (root / "pages" / filename).write_text(content, encoding="utf-8")
            shim = binary / "command-shim"
            shim.write_text(f"#!{sys.executable}\n" + textwrap.dedent('''\
                import json
                import os
                import sys
                from pathlib import Path
                command = Path(sys.argv[0]).name
                args = sys.argv[1:]
                with open("commands.jsonl", "a", encoding="utf-8") as output:
                    output.write(json.dumps([command, *args]) + "\\n")
                if command == "python3" and args[0] == "scripts/generate-image-catalog.py":
                    changed = os.environ["TEST_CHANGED"] == "1"
                    Path("generated/catalog.json").write_text('{"updated": true}\\n' if changed else '{}\\n')
                    Path("generated/catalog-data.js").write_text('window.IMAGE_CATALOG = {updated: true};\\n' if changed else 'window.IMAGE_CATALOG = {};\\n')
                if command == "git" and "diff" in args:
                    sys.exit(1 if os.environ["TEST_CHANGED"] == "1" else 0)
                '''), encoding="utf-8")
            shim.chmod(0o755)
            for command in ("python3", "git", "node"):
                (binary / command).symlink_to(shim)
            result = subprocess.run(
                ["bash", "-c", script], cwd=root, text=True, capture_output=True,
                env={**os.environ, "PATH": f"{binary}:{os.environ['PATH']}", "EVENT_NAME": event,
                     "REQUESTED_MODE": mode, "PUBLISH_SUMMARY": "publish-artifact/summary with spaces.json",
                     "TEST_CHANGED": "1" if changed else "0"},
            )
            log = root / "commands.jsonl"
            commands = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
            return result, commands

    def test_event_routing_and_no_change_do_not_commit_or_push(self) -> None:
        for event, requested, expected in (("push", "full", "incremental"),
                                            ("workflow_dispatch", "full", "full"),
                                            ("workflow_dispatch", "incremental", "incremental"),
                                            ("workflow_run", "full", "publish")):
            with self.subTest(event=event, requested=requested):
                result, commands = self.run_render(event, requested)
                self.assertEqual(result.returncode, 0, result.stderr)
                expected_command = ["python3", "scripts/generate-image-catalog.py", "--mode", expected,
                                    "--baseline", "pages/catalog.json", "--output", "generated/catalog.json"]
                if event == "workflow_run":
                    expected_command += ["--publish-summary", "publish-artifact/summary with spaces.json"]
                self.assertEqual(commands[0], expected_command)
                self.assertFalse(any(command[0] == "git" or "scripts/network_retry.py" in command
                                     for command in commands))

    def test_changed_catalog_commits_both_files_and_retries_same_push(self) -> None:
        result, commands = self.run_render("workflow_dispatch", changed=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(["git", "-C", "pages", "add", "--", "catalog.json", "catalog-data.js"], commands)
        self.assertIn(["git", "-C", "pages", "commit", "-m", "docs: update published image catalog"], commands)
        self.assertEqual(commands[-1], ["python3", "scripts/network_retry.py", "--timeout", "300", "--",
                                        "git", "-C", "pages", "push", "origin", "HEAD:refs/heads/gh-pages"])

    def test_unsupported_event_fails_before_rendering(self) -> None:
        result, commands = self.run_render("pull_request")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unsupported catalog event", result.stderr)
        self.assertEqual(commands, [])


if __name__ == "__main__":
    unittest.main()
