from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VALIDATE_SUMMARY = ROOT / "scripts" / "validate-publish-summary.py"
PROFILE_PATH = ROOT / "config" / "profiles" / "core.json"


def digest(index: int) -> str:
    return f"sha256:{index:064x}"


def core_profile() -> dict:
    return json.loads(PROFILE_PATH.read_text(encoding="utf-8"))


def publish_summary(
    *,
    distro: str = "rocky",
    distro_version: str = "9",
    images: list[dict] | None = None,
) -> dict:
    tag = f"2025.1-{distro}-{distro_version}"
    profile_images = core_profile()["images"]
    selected_images = images or profile_images
    return {
        "release": "2025.1",
        "distro": distro,
        "distro_version": distro_version,
        "profile": "core",
        "registry": "ghcr.io",
        "owner": "supergate-jhbyun",
        "repository": "kolla-container-images",
        "images": [
            {
                "image": image["name"],
                "kolla_ansible_variables": image["kolla_ansible_variables"],
                "deploy_ref": (
                    "ghcr.io/supergate-jhbyun/kolla-container-images/"
                    f"{image['name']}:{tag}"
                ),
                "deploy_tag": tag,
                "manifest_digest": digest(index + 1),
                "architectures": [
                    {
                        "arch": arch,
                        "platform": f"linux/{arch}",
                        "arch_ref": (
                            "ghcr.io/supergate-jhbyun/kolla-container-images/"
                            f"{image['name']}:{tag}-{arch}"
                        ),
                    }
                    for arch in ("amd64", "arm64")
                ],
            }
            for index, image in enumerate(selected_images)
        ],
    }


def run_validator(summary_path: Path, *extra_args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(VALIDATE_SUMMARY),
            "--publish-summary",
            str(summary_path),
            "--profile",
            "core",
            "--release",
            "2025.1",
            "--distro",
            "rocky",
            "--distro-version",
            "9",
            *extra_args,
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )


class PublishSummaryValidationTest(unittest.TestCase):
    def test_valid_full_core_summary_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            summary_path = Path(temp_dir) / "publish-summary.json"
            summary_path.write_text(json.dumps(publish_summary()), encoding="utf-8")

            result = run_validator(summary_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Publish summary validation passed.", result.stdout)

    def test_missing_digest_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            summary = publish_summary()
            del summary["images"][0]["manifest_digest"]
            summary_path = Path(temp_dir) / "publish-summary.json"
            summary_path.write_text(json.dumps(summary), encoding="utf-8")

            result = run_validator(summary_path)

            self.assertEqual(result.returncode, 1)
            self.assertIn("manifest_digest must be sha256", result.stderr)

    def test_distro_mismatch_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            summary_path = Path(temp_dir) / "publish-summary.json"
            summary_path.write_text(
                json.dumps(publish_summary(distro="ubuntu", distro_version="24.04")),
                encoding="utf-8",
            )

            result = run_validator(summary_path)

            self.assertEqual(result.returncode, 1)
            self.assertIn("publish summary distro must be 'rocky'", result.stderr)

    def test_partial_summary_without_partial_flag_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            keystone = core_profile()["images"][0]
            summary_path = Path(temp_dir) / "publish-summary.json"
            summary_path.write_text(
                json.dumps(publish_summary(images=[keystone])),
                encoding="utf-8",
            )

            result = run_validator(summary_path)

            self.assertEqual(result.returncode, 1)
            self.assertIn("publish summary is missing image:", result.stderr)

    def test_missing_architecture_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            summary = publish_summary()
            summary["images"][0]["architectures"].pop()
            summary_path = Path(temp_dir) / "publish-summary.json"
            summary_path.write_text(json.dumps(summary), encoding="utf-8")

            result = run_validator(summary_path)

            self.assertEqual(result.returncode, 1)
            self.assertIn("architectures must be exactly", result.stderr)

    def test_partial_keystone_summary_passes_with_explicit_flag(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            keystone = core_profile()["images"][0]
            summary_path = Path(temp_dir) / "publish-summary.json"
            summary_path.write_text(
                json.dumps(publish_summary(images=[keystone])),
                encoding="utf-8",
            )

            result = run_validator(summary_path, "--allow-partial", "--image", "keystone")

            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
