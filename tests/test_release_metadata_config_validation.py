from __future__ import annotations

import copy
import runpy
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RELEASES_REPOSITORY = "https://opendev.org/openstack/releases"
KOLLA_COMMIT = "99b84ab9b9223b10130e3b5da5c8dc00f6e01ef5"
KOLLA_ANSIBLE_COMMIT = "0786e1d6bd9a6da2d8ae15cc16a891bef0d32696"


class ReleaseMetadataConfigValidationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.validator = runpy.run_path(str(ROOT / "scripts" / "validate-config.py"))

    def git(self, repository: Path, *arguments: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout.strip()

    def create_releases_checkout(self, root: Path) -> tuple[Path, str]:
        checkout = root / "releases"
        checkout.mkdir()
        self.git(checkout, "init", "--quiet")
        self.git(checkout, "config", "user.name", "Release Metadata Test")
        self.git(
            checkout,
            "config",
            "user.email",
            "release-metadata-test@example.invalid",
        )
        deliverables = checkout / "deliverables" / "epoxy"
        deliverables.mkdir(parents=True)
        (deliverables / "kolla.yaml").write_text(
            """---
launchpad: kolla
releases:
  - version: 20.4.0
    projects:
      - repo: openstack/kolla
        hash: 99b84ab9b9223b10130e3b5da5c8dc00f6e01ef5
branches:
  - name: stable/2025.1
""",
            encoding="utf-8",
        )
        (deliverables / "kolla-ansible.yaml").write_text(
            """---
launchpad: kolla-ansible
releases:
  - version: 20.4.0
    projects:
      - repo: openstack/kolla-ansible
        hash: 0786e1d6bd9a6da2d8ae15cc16a891bef0d32696
branches:
  - name: stable/2025.1
""",
            encoding="utf-8",
        )
        self.git(checkout, "add", "deliverables")
        self.git(checkout, "commit", "--quiet", "-m", "release metadata fixture")
        commit = self.git(checkout, "rev-parse", "HEAD")
        self.git(checkout, "remote", "add", "origin", RELEASES_REPOSITORY)
        return checkout, commit

    def matrix(self, metadata_commit: str) -> dict[str, object]:
        return {
            "release_metadata": {
                "repository": RELEASES_REPOSITORY,
                "commit": metadata_commit,
            },
            "releases": {
                "2025.1": {
                    "series": "epoxy",
                    "source_set": "epoxy-20260813-r1",
                }
            },
            "toolchains": {
                "20.4.0": {
                    "kolla": {
                        "repository": "https://opendev.org/openstack/kolla",
                        "commit": KOLLA_COMMIT,
                    },
                    "kolla_ansible": {
                        "repository": (
                            "https://opendev.org/openstack/kolla-ansible"
                        ),
                        "commit": KOLLA_ANSIBLE_COMMIT,
                    },
                }
            },
            "streams": [
                {
                    "id": "2025.1-rocky-10.2-20.4.0",
                    "release": "2025.1",
                    "toolchain": "20.4.0",
                    "base": "rocky-10.2",
                    "publish_enabled": True,
                }
            ],
        }

    def validate(self, matrix: dict[str, object], checkout: Path) -> list[str]:
        errors: list[str] = []
        self.validator["validate_release_metadata_toolchain_pins"](
            matrix,
            checkout,
            errors,
        )
        return errors

    def test_accepts_exact_pins_from_verified_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            checkout, commit = self.create_releases_checkout(Path(temp_dir))

            self.assertEqual(self.validate(self.matrix(commit), checkout), [])

    def test_rejects_each_matrix_commit_that_differs_from_releases_metadata(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            checkout, commit = self.create_releases_checkout(Path(temp_dir))
            matrix = self.matrix(commit)
            cases = (
                ("kolla", "a" * 40),
                ("kolla_ansible", "b" * 40),
            )
            for project, replacement in cases:
                with self.subTest(project=project):
                    mutated = copy.deepcopy(matrix)
                    mutated["toolchains"]["20.4.0"][project]["commit"] = replacement

                    errors = self.validate(mutated, checkout)

                    self.assertTrue(
                        any(
                            f"toolchains['20.4.0'].{project}.commit" in error
                            and "does not match pinned OpenStack Releases metadata"
                            in error
                            for error in errors
                        ),
                        errors,
                    )

    def test_rejects_a_version_absent_from_releases_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            checkout, commit = self.create_releases_checkout(Path(temp_dir))
            matrix = self.matrix(commit)
            matrix["toolchains"]["20.5.0"] = matrix["toolchains"].pop("20.4.0")
            matrix["streams"][0]["toolchain"] = "20.5.0"

            errors = self.validate(matrix, checkout)

            self.assertTrue(
                any(
                    "toolchain '20.5.0'" in error
                    and "OpenStack Releases metadata" in error
                    for error in errors
                ),
                errors,
            )

    def test_rejects_checkout_not_at_the_matrix_metadata_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            checkout, commit = self.create_releases_checkout(Path(temp_dir))
            matrix = self.matrix(commit)
            marker = checkout / "new-release.txt"
            marker.write_text("new\n", encoding="utf-8")
            self.git(checkout, "add", marker.name)
            self.git(checkout, "commit", "--quiet", "-m", "new metadata")

            errors = self.validate(matrix, checkout)

            self.assertTrue(
                any(
                    "release metadata checkout is invalid" in error
                    and "does not match frozen commit" in error
                    for error in errors
                ),
                errors,
            )

    def test_rejects_dirty_or_wrong_origin_checkout(self) -> None:
        for mutation in ("dirty", "origin"):
            with (
                self.subTest(mutation=mutation),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                checkout, commit = self.create_releases_checkout(Path(temp_dir))
                if mutation == "dirty":
                    (checkout / "deliverables" / "epoxy" / "kolla.yaml").write_text(
                        "mutated\n",
                        encoding="utf-8",
                    )
                else:
                    self.git(
                        checkout,
                        "remote",
                        "set-url",
                        "origin",
                        "https://example.invalid/openstack/releases",
                    )

                errors = self.validate(self.matrix(commit), checkout)

                self.assertTrue(
                    any(
                        "release metadata checkout is invalid" in error
                        for error in errors
                    ),
                    errors,
                )


if __name__ == "__main__":
    unittest.main()
