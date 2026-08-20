from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "sync-publish-stack-pr.py"


def load_stack_sync_module():
    spec = importlib.util.spec_from_file_location("sync_publish_stack_pr", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


STACK_SYNC = load_stack_sync_module()


class StackSyncTest(unittest.TestCase):
    def request(self, repository_dir: Path):
        return STACK_SYNC.StackRequest(
            repository="supergate-hub/kolla-container-images",
            head_sha="a" * 40,
            source_branch="feat/catalog",
            pull_request_number="42",
            repository_dir=repository_dir,
            app_token="app-token",
        )

    def test_request_validation_rejects_untrusted_identity_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            request = self.request(Path(temp_dir))
            for invalid in (
                replace(request, repository="owner/name?ref=main"),
                replace(request, head_sha="A" * 40),
                replace(request, source_branch="feat/catalog\nmain"),
                replace(request, pull_request_number="0"),
                replace(request, app_token=""),
            ):
                with self.subTest(request=invalid):
                    with self.assertRaises(STACK_SYNC.StackSyncError):
                        STACK_SYNC.validate_request(invalid)

    def test_command_errors_redact_the_app_token(self) -> None:
        secret = "top-secret-app-token"
        failure = subprocess.CompletedProcess(
            args=["git"],
            returncode=1,
            stdout="",
            stderr=f"remote rejected {secret}",
        )
        with patch.object(STACK_SYNC.subprocess, "run", return_value=failure):
            with self.assertRaises(STACK_SYNC.StackSyncError) as captured:
                STACK_SYNC.run_command(
                    ["git", "push", f"https://x-access-token:{secret}@github.com/repo"],
                    secrets=(secret,),
                )
        self.assertNotIn(secret, str(captured.exception))
        self.assertIn("***", str(captured.exception))

    def test_synchronized_proposal_closes_an_existing_stack_pr(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            request = self.request(Path(temp_dir))
            with (
                patch.object(
                    STACK_SYNC,
                    "proposal_content",
                    side_effect=("{}", "name: Kolla publish\n"),
                ),
                patch.object(STACK_SYNC, "render_dropdown"),
                patch.object(
                    STACK_SYNC, "existing_stack_pull_request", return_value="78"
                ),
                patch.object(STACK_SYNC, "close_redundant_stack_pull_request") as close,
            ):
                self.assertFalse(STACK_SYNC.synchronize_stack_pull_request(request))
        close.assert_called_once_with("78", request)

    def test_changed_proposal_creates_or_refreshes_the_stack_pr(self) -> None:
        rendered = {}

        def render_changed_dropdown(_, __, workflow_path: Path) -> None:
            workflow_path.write_text("name: generated dropdown\n", encoding="utf-8")

        def record_stack_commit(_, workflow_path: Path) -> None:
            rendered["content"] = workflow_path.read_text(encoding="utf-8")

        with tempfile.TemporaryDirectory() as temp_dir:
            request = self.request(Path(temp_dir))
            with (
                patch.object(
                    STACK_SYNC,
                    "proposal_content",
                    side_effect=("{}", "name: Kolla publish\n"),
                ),
                patch.object(
                    STACK_SYNC, "render_dropdown", side_effect=render_changed_dropdown
                ),
                patch.object(
                    STACK_SYNC, "existing_stack_pull_request", return_value=None
                ),
                patch.object(
                    STACK_SYNC, "create_stack_commit", side_effect=record_stack_commit
                ) as commit,
                patch.object(
                    STACK_SYNC, "create_or_update_stack_pull_request"
                ) as pull_request,
            ):
                self.assertTrue(STACK_SYNC.synchronize_stack_pull_request(request))
                self.assertEqual(commit.call_args.args[0], request)
        self.assertEqual(rendered["content"], "name: generated dropdown\n")
        pull_request.assert_called_once_with(None, request)


if __name__ == "__main__":
    unittest.main()
