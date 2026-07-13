from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VALIDATE_APPROVAL = ROOT / "scripts" / "validate-publish-approval.py"
SMOKE_APPROVAL = (
    "I approve GHCR smoke publish for keystone 2025.1-rocky-9 from "
    "supergate-jhbyun/kolla-container-images."
)
FULL_CORE_APPROVAL = (
    "I approve GHCR full-core publish for core 2025.1-rocky-9 "
    "(21 images, amd64/arm64) from supergate-jhbyun/kolla-container-images."
)
DEPLOYMENT_APPROVAL = (
    "I approve GHCR deployment publish for deployment 2025.1-rocky-9 "
    "(52 images, amd64/arm64) from supergate-jhbyun/kolla-container-images."
)


def run_validator(
    *,
    image: str,
    approval: str,
    allow_smoke: str = "false",
    allow_full_core: str = "false",
    allow_deployment: str = "false",
    profile: str = "core",
    distro: str = "rocky",
    distro_version: str = "9",
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(
        {
            "ALLOW_GHCR_PUBLISH": allow_smoke,
            "ALLOW_GHCR_FULL_CORE_PUBLISH": allow_full_core,
            "ALLOW_GHCR_DEPLOYMENT_PUBLISH": allow_deployment,
            "APPROVAL": approval,
        }
    )
    return subprocess.run(
        [
            sys.executable,
            str(VALIDATE_APPROVAL),
            "--profile",
            profile,
            "--image",
            image,
            "--release",
            "2025.1",
            "--distro",
            distro,
            "--distro-version",
            distro_version,
        ],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
    )


class PublishApprovalTest(unittest.TestCase):
    def test_keystone_smoke_scope_still_passes(self) -> None:
        result = run_validator(
            image="keystone",
            approval=SMOKE_APPROVAL,
            allow_smoke="true",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Smoke publish approval validated.", result.stdout)

    def test_approved_full_core_scope_passes(self) -> None:
        result = run_validator(
            image="all",
            approval=FULL_CORE_APPROVAL,
            allow_full_core="true",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Full-core publish approval validated.", result.stdout)

    def test_full_core_requires_dedicated_variable(self) -> None:
        result = run_validator(image="all", approval=FULL_CORE_APPROVAL)

        self.assertEqual(result.returncode, 1)
        self.assertIn("ALLOW_GHCR_FULL_CORE_PUBLISH=true", result.stderr)

    def test_full_core_rejects_wrong_approval_phrase(self) -> None:
        result = run_validator(
            image="all",
            approval=SMOKE_APPROVAL,
            allow_full_core="true",
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("exact full-core publish approval phrase", result.stderr)

    def test_smoke_variable_does_not_authorize_full_core(self) -> None:
        result = run_validator(
            image="all",
            approval=FULL_CORE_APPROVAL,
            allow_smoke="true",
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("ALLOW_GHCR_FULL_CORE_PUBLISH=true", result.stderr)

    def test_scope_change_is_rejected(self) -> None:
        result = run_validator(
            image="all",
            approval=FULL_CORE_APPROVAL,
            allow_full_core="true",
            distro="ubuntu",
            distro_version="24.04",
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("not approved for real publish", result.stderr)

    def test_approved_deployment_scope_passes(self) -> None:
        result = run_validator(
            profile="deployment",
            image="all",
            approval=DEPLOYMENT_APPROVAL,
            allow_deployment="true",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Deployment publish approval validated.", result.stdout)

    def test_deployment_requires_dedicated_variable(self) -> None:
        result = run_validator(
            profile="deployment",
            image="all",
            approval=DEPLOYMENT_APPROVAL,
            allow_full_core="true",
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("ALLOW_GHCR_DEPLOYMENT_PUBLISH=true", result.stderr)

    def test_deployment_rejects_wrong_phrase_and_partial_scope(self) -> None:
        wrong_phrase = run_validator(
            profile="deployment",
            image="all",
            approval=FULL_CORE_APPROVAL,
            allow_deployment="true",
        )
        partial_scope = run_validator(
            profile="deployment",
            image="keystone",
            approval=DEPLOYMENT_APPROVAL,
            allow_deployment="true",
        )

        self.assertEqual(wrong_phrase.returncode, 1)
        self.assertIn("exact deployment publish approval phrase", wrong_phrase.stderr)
        self.assertEqual(partial_scope.returncode, 1)
        self.assertIn("not approved for real publish", partial_scope.stderr)


if __name__ == "__main__":
    unittest.main()
