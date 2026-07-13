from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from scripts.profile_resolver import (
    find_stream,
    load_matrix,
    load_profile,
    render_candidate_tag,
    render_tag,
    resolve_profile,
    selector_matches,
    stream_ids,
    validate_candidate_id,
)


class ProfileResolutionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.ubuntu_2026 = {
            "id": "alpha",
            "release": "2026.1",
            "distro": "ubuntu",
            "base_tag": "24.04",
            "tag_token": "noble",
        }
        self.rocky_2025 = {
            "id": "beta",
            "release": "2025.1",
            "distro": "rocky",
            "base_tag": "9",
            "tag_token": "9",
        }
        self.matrix = {
            "schema_version": 2,
            "streams": [self.ubuntu_2026, self.rocky_2025],
            "tag_policy": {
                "deploy_tag_template": "{release}-{distro}-{tag_token}",
                "candidate_tag_template": (
                    "{release}-{distro}-{tag_token}-candidate-{candidate_id}"
                ),
                "candidate_arch_tag_template": (
                    "{release}-{distro}-{tag_token}-candidate-{candidate_id}-{arch}"
                ),
            },
        }
        self.profile = {
            "schema_version": 3,
            "name": "sample",
            "reviewed_streams": ["alpha", "beta"],
            "images": [
                {
                    "name": "always",
                    "kolla_ansible_variables": [
                        "always_image_full",
                        {
                            "name": "new_alias_image_full",
                            "applies_to": {"releases": ["2026.1"]},
                        },
                        {
                            "name": "rocky_alias_image_full",
                            "applies_to": {"distros": ["rocky"]},
                        },
                    ],
                },
                {
                    "name": "rocky-only",
                    "kolla_ansible_variables": ["rocky_only_image_full"],
                    "applies_to": {"streams": ["beta"]},
                },
                {
                    "name": "ubuntu-only",
                    "kolla_ansible_variables": ["ubuntu_only_image_full"],
                    "applies_to": {"distros": ["ubuntu"]},
                },
                {
                    "name": "new-ubuntu-only",
                    "kolla_ansible_variables": ["new_ubuntu_only_image_full"],
                    "applies_to": {
                        "releases": ["2026.1"],
                        "distros": ["ubuntu"],
                    },
                },
            ],
            "build_groups": [
                {
                    "name": "mixed",
                    "parents": ["base", "openstack-base"],
                    "images": ["ubuntu-only", "always", "rocky-only"],
                },
                {
                    "name": "rocky",
                    "parents": ["base", "rocky-base"],
                    "images": ["rocky-only"],
                },
                {
                    "name": "new",
                    "parents": ["base", "prometheus-base"],
                    "images": ["new-ubuntu-only"],
                    "applies_to": {"releases": ["2026.1"]},
                },
                {
                    "name": "legacy",
                    "parents": ["base", "legacy-base"],
                    "images": ["always"],
                    "applies_to": {"releases": ["2025.1"]},
                },
            ],
        }

    def test_loaders_read_json_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            matrix_path = temp_path / "matrix.json"
            profiles_dir = temp_path / "profiles"
            profiles_dir.mkdir()
            matrix_path.write_text(json.dumps(self.matrix), encoding="utf-8")
            (profiles_dir / "sample.json").write_text(
                json.dumps(self.profile),
                encoding="utf-8",
            )

            self.assertEqual(load_matrix(matrix_path), self.matrix)
            self.assertEqual(load_profile("sample", profiles_dir), self.profile)

    def test_stream_ids_and_find_stream_preserve_matrix_order(self) -> None:
        self.assertEqual(stream_ids(self.matrix), ["alpha", "beta"])
        self.assertIs(find_stream(self.matrix, "beta"), self.rocky_2025)

    def test_find_stream_lists_accepted_ids_on_failure(self) -> None:
        with self.assertRaisesRegex(ValueError, "accepted streams: alpha, beta"):
            find_stream(self.matrix, "missing")

    def test_selector_without_applies_to_matches(self) -> None:
        self.assertTrue(selector_matches(None, self.ubuntu_2026))

    def test_selector_dimensions_are_anded(self) -> None:
        selector = {"releases": ["2026.1"], "distros": ["ubuntu"]}
        self.assertTrue(selector_matches(selector, self.ubuntu_2026))
        self.assertFalse(selector_matches(selector, self.rocky_2025))

    def test_architecture_selector_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported applies_to keys"):
            selector_matches({"architectures": ["arm64"]}, self.ubuntu_2026)

    def test_empty_selector_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "applies_to must not be empty"):
            selector_matches({}, self.ubuntu_2026)

    def test_resolve_profile_returns_filtered_plain_json_in_original_order(self) -> None:
        original = copy.deepcopy(self.profile)

        resolved = resolve_profile(self.profile, self.ubuntu_2026)

        self.assertEqual(
            resolved,
            {
                "schema_version": 3,
                "name": "sample",
                "reviewed_streams": ["alpha", "beta"],
                "images": [
                    {
                        "name": "always",
                        "kolla_ansible_variables": [
                            "always_image_full",
                            "new_alias_image_full",
                        ],
                    },
                    {
                        "name": "ubuntu-only",
                        "kolla_ansible_variables": ["ubuntu_only_image_full"],
                    },
                    {
                        "name": "new-ubuntu-only",
                        "kolla_ansible_variables": [
                            "new_ubuntu_only_image_full"
                        ],
                    },
                ],
                "build_groups": [
                    {
                        "name": "mixed",
                        "parents": ["base", "openstack-base"],
                        "images": ["ubuntu-only", "always"],
                    },
                    {
                        "name": "new",
                        "parents": ["base", "prometheus-base"],
                        "images": ["new-ubuntu-only"],
                    },
                ],
                "resolved_stream": "alpha",
            },
        )
        self.assertEqual(json.loads(json.dumps(resolved)), resolved)
        self.assertEqual(self.profile, original)

    def test_build_group_selector_changes_parent_chain_by_stream(self) -> None:
        ubuntu = resolve_profile(self.profile, self.ubuntu_2026)
        rocky = resolve_profile(self.profile, self.rocky_2025)

        self.assertEqual(
            [group["name"] for group in ubuntu["build_groups"]],
            ["mixed", "new"],
        )
        self.assertEqual(
            [group["name"] for group in rocky["build_groups"]],
            ["mixed", "rocky", "legacy"],
        )
        self.assertTrue(
            all("applies_to" not in group for group in ubuntu["build_groups"])
        )
        self.assertTrue(
            all("applies_to" not in group for group in rocky["build_groups"])
        )

    def test_unreviewed_stream_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "has not reviewed stream"):
            resolve_profile(self.profile, {**self.rocky_2025, "id": "unreviewed"})

    def test_profile_schema_must_be_three(self) -> None:
        with self.assertRaisesRegex(ValueError, "schema_version must be 3"):
            resolve_profile({**self.profile, "schema_version": 2}, self.ubuntu_2026)

    def test_render_stream_and_candidate_tags_are_explicitly_separate(self) -> None:
        self.assertEqual(
            render_tag(self.matrix, self.ubuntu_2026),
            "2026.1-ubuntu-noble",
        )
        self.assertEqual(
            render_tag(self.matrix, self.ubuntu_2026, "arm64"),
            "2026.1-ubuntu-noble-arm64",
        )
        self.assertEqual(
            render_candidate_tag(
                self.matrix, self.ubuntu_2026, "123456789-1"
            ),
            "2026.1-ubuntu-noble-candidate-123456789-1",
        )
        self.assertEqual(
            render_candidate_tag(
                self.matrix, self.ubuntu_2026, "123456789-1", "arm64"
            ),
            "2026.1-ubuntu-noble-candidate-123456789-1-arm64",
        )

    def test_candidate_id_validation_rejects_non_run_shapes(self) -> None:
        self.assertEqual(validate_candidate_id("local-dry-run"), "local-dry-run")
        self.assertEqual(validate_candidate_id("123456789-1"), "123456789-1")
        for value in ("", "0-1", "1-0", "01-1", "1-01", "1", "1-a", True):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_candidate_id(value)
        with self.assertRaisesRegex(ValueError, "workflow candidate ID"):
            validate_candidate_id("local-dry-run", allow_local=False)


if __name__ == "__main__":
    unittest.main()
