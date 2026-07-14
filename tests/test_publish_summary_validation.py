from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.profile_resolver import (
    find_stream,
    load_matrix,
    load_profile,
    render_candidate_tag,
    resolve_profile,
)


ROOT = Path(__file__).resolve().parents[1]
VALIDATE_SUMMARY = ROOT / "scripts" / "validate-publish-summary.py"
MATRIX = load_matrix()
TEST_CANDIDATE_ID = "123456789-1"
candidate_tag = "2025.1-rocky-9-candidate-123456789-1"
candidate_ref = (
    "ghcr.io/supergate-hub/kolla-container-images/keystone:"
    + candidate_tag
)
amd64_ref = candidate_ref + "-amd64"
arm64_ref = candidate_ref + "-arm64"
stream_ref = (
    "ghcr.io/supergate-hub/kolla-container-images/keystone:2025.1-rocky-9"
)
STREAM_COUNTS = {
    "2025.1-rocky-9": 63,
    "2025.1-rocky-10": 63,
    "2025.1-ubuntu-noble": 64,
    "2025.2-rocky-10": 63,
    "2025.2-ubuntu-noble": 64,
    "2026.1-rocky-10": 65,
    "2026.1-ubuntu-noble": 66,
}


def digest(index: int) -> str:
    return f"sha256:{index:064x}"


def resolved_profile(stream_id: str, profile_name: str) -> tuple[dict, dict]:
    stream = find_stream(MATRIX, stream_id)
    profile = resolve_profile(load_profile(profile_name), stream)
    return stream, profile


def summary_image(stream: dict, profile_image: dict, index: int) -> dict:
    image = profile_image["name"]
    deploy_tag = render_candidate_tag(MATRIX, stream, TEST_CANDIDATE_ID)
    repository = (
        f"{MATRIX['registry']}/{MATRIX['owner']}/{MATRIX['repository']}/{image}"
    )
    return {
        "image": image,
        "kolla_ansible_variables": profile_image["kolla_ansible_variables"],
        "deploy_tag": deploy_tag,
        "deploy_ref": f"{repository}:{deploy_tag}",
        "manifest_digest": digest(index * 10 + 9),
        "architectures": [
            {
                "arch": arch,
                "platform": f"linux/{arch}",
                "arch_ref": (
                    f"{repository}:"
                    f"{render_candidate_tag(MATRIX, stream, TEST_CANDIDATE_ID, arch)}"
                ),
                "digest": digest(index * 10 + arch_index + 1),
            }
            for arch_index, arch in enumerate(MATRIX["architectures"])
        ],
    }


def publish_summary(
    stream_id: str = "2025.1-rocky-9",
    profile_name: str = "deployment",
    image_filter: str | None = None,
) -> dict:
    stream, profile = resolved_profile(stream_id, profile_name)
    selected_images = profile["images"]
    if image_filter is not None:
        selected_images = [
            image for image in selected_images if image["name"] == image_filter
        ]
        if not selected_images:
            raise ValueError(
                f"image does not exist in profile {profile_name}: {image_filter}"
            )
    return {
        "candidate_id": TEST_CANDIDATE_ID,
        "stream": stream["id"],
        "release": stream["release"],
        "distro": stream["distro"],
        "distro_version": stream["base_tag"],
        "profile": profile["name"],
        "scope": {
            "profile": profile["name"],
            "image": image_filter or "all",
            "image_count": len(selected_images),
        },
        "registry": MATRIX["registry"],
        "owner": MATRIX["owner"],
        "repository": MATRIX["repository"],
        "images": [
            summary_image(stream, image, index)
            for index, image in enumerate(selected_images, start=1)
        ],
    }


def image_entry(summary: dict, image: str) -> dict:
    return next(entry for entry in summary["images"] if entry["image"] == image)


def duplicate_key_summary_json() -> dict[str, str]:
    raw = json.dumps(publish_summary())
    return {
        "root": raw.replace(
            '{"candidate_id": ',
            '{"candidate_id": "ignored", "candidate_id": ',
            1,
        ),
        "scope": raw.replace(
            '"scope": {"profile": ',
            '"scope": {"profile": "ignored", "profile": ',
            1,
        ),
        "image": raw.replace(
            '"images": [{"image": ',
            '"images": [{"image": "ignored", "image": ',
            1,
        ),
        "architecture": raw.replace(
            '"architectures": [{"arch": ',
            '"architectures": [{"arch": "ignored", "arch": ',
            1,
        ),
    }


def run_validator_json(
    summary_json: str,
    *,
    stream: str = "2025.1-rocky-9",
    profile: str = "deployment",
    candidate_id: str = TEST_CANDIDATE_ID,
    allow_partial: bool = False,
    image: str | None = None,
) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory() as temp_dir:
        summary_path = Path(temp_dir) / "publish-summary.json"
        summary_path.write_text(summary_json, encoding="utf-8")
        command = [
            sys.executable,
            str(VALIDATE_SUMMARY),
            "--publish-summary",
            str(summary_path),
            "--stream",
            stream,
            "--profile",
            profile,
            "--candidate-id",
            candidate_id,
        ]
        if allow_partial:
            command.append("--allow-partial")
        if image is not None:
            command.extend(["--image", image])
        return subprocess.run(
            command,
            cwd=ROOT,
            text=True,
            capture_output=True,
        )


def run_validator(
    summary: dict,
    *,
    stream: str = "2025.1-rocky-9",
    profile: str = "deployment",
    candidate_id: str = TEST_CANDIDATE_ID,
    allow_partial: bool = False,
    image: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return run_validator_json(
        json.dumps(summary),
        stream=stream,
        profile=profile,
        candidate_id=candidate_id,
        allow_partial=allow_partial,
        image=image,
    )


class PublishSummaryValidationTest(unittest.TestCase):
    def test_stable_stream_ref_is_rejected_as_candidate_deploy_ref(self) -> None:
        summary = publish_summary()
        entry = image_entry(summary, "keystone")
        entry["deploy_tag"] = "2025.1-rocky-9"
        entry["deploy_ref"] = stream_ref
        result = run_validator(summary)
        self.assertEqual(result.returncode, 1)
        self.assertIn("candidate-123456789-1", result.stderr)

    def test_summary_candidate_id_must_match_expected_id(self) -> None:
        summary = publish_summary()
        summary["candidate_id"] = "123456789-2"
        result = run_validator(summary)
        self.assertEqual(result.returncode, 1)
        self.assertIn("candidate_id must be '123456789-1'", result.stderr)

    def test_malformed_expected_candidate_id_is_rejected(self) -> None:
        result = run_validator(publish_summary(), candidate_id="01-1")
        self.assertEqual(result.returncode, 2)
        self.assertIn("candidate ID", result.stderr)

    def test_full_deployment_summaries_pass_representative_streams(self) -> None:
        for stream_id in (
            "2025.1-rocky-9",
            "2025.1-ubuntu-noble",
            "2026.1-ubuntu-noble",
        ):
            with self.subTest(stream=stream_id):
                result = run_validator(publish_summary(stream_id), stream=stream_id)

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Publish summary validation passed.", result.stdout)

    def test_all_streams_enforce_exact_resolved_image_count(self) -> None:
        for stream_id, expected_count in STREAM_COUNTS.items():
            with self.subTest(stream=stream_id):
                summary = publish_summary(stream_id)
                self.assertEqual(len(summary["images"]), expected_count)
                self.assertEqual(summary["scope"]["image_count"], expected_count)

                summary["scope"]["image_count"] += 1
                result = run_validator(summary, stream=stream_id)

                self.assertEqual(result.returncode, 1)
                self.assertIn("publish summary scope must be", result.stderr)

    def test_missing_and_extra_conditional_leaves_fail(self) -> None:
        ubuntu_summary = publish_summary("2025.1-ubuntu-noble")
        ubuntu_summary["images"] = [
            image for image in ubuntu_summary["images"] if image["image"] != "tgtd"
        ]
        missing_result = run_validator(
            ubuntu_summary,
            stream="2025.1-ubuntu-noble",
        )

        self.assertEqual(missing_result.returncode, 1)
        self.assertIn("publish summary is missing image: tgtd", missing_result.stderr)

        rocky_stream, _ = resolved_profile("2025.1-rocky-9", "deployment")
        _, ubuntu_profile = resolved_profile("2025.1-ubuntu-noble", "deployment")
        tgtd = next(image for image in ubuntu_profile["images"] if image["name"] == "tgtd")
        rocky_summary = publish_summary("2025.1-rocky-9")
        rocky_summary["images"].append(summary_image(rocky_stream, tgtd, 100))
        extra_result = run_validator(rocky_summary)

        self.assertEqual(extra_result.returncode, 1)
        self.assertIn("publish summary contains unexpected image: tgtd", extra_result.stderr)

    def test_parent_and_duplicate_leaves_are_rejected(self) -> None:
        stream, _ = resolved_profile("2025.1-rocky-9", "deployment")
        cases = []

        parent_summary = publish_summary()
        parent_summary["images"].append(
            summary_image(
                stream,
                {"name": "base", "kolla_ansible_variables": []},
                100,
            )
        )
        cases.append(("parent", parent_summary, "unexpected image: base"))

        duplicate_summary = publish_summary()
        duplicate_summary["images"].append(
            copy.deepcopy(duplicate_summary["images"][0])
        )
        cases.append(("duplicate", duplicate_summary, "duplicate image"))

        for name, summary, expected_error in cases:
            with self.subTest(case=name):
                result = run_validator(summary)

                self.assertEqual(result.returncode, 1)
                self.assertIn(expected_error, result.stderr)

    def test_top_level_and_scope_mismatches_are_rejected(self) -> None:
        mutations = {
            "stream": lambda summary: summary.__setitem__("stream", "2025.1-rocky-10"),
            "release": lambda summary: summary.__setitem__("release", "2025.2"),
            "distro": lambda summary: summary.__setitem__("distro", "ubuntu"),
            "distro_version": lambda summary: summary.__setitem__(
                "distro_version", "10"
            ),
            "registry": lambda summary: summary.__setitem__(
                "registry", "registry.example.invalid"
            ),
            "owner": lambda summary: summary.__setitem__("owner", "wrong-owner"),
            "repository": lambda summary: summary.__setitem__(
                "repository", "wrong-repository"
            ),
            "profile": lambda summary: summary.__setitem__("profile", "core"),
            "scope_profile": lambda summary: summary["scope"].__setitem__(
                "profile", "core"
            ),
            "scope_image": lambda summary: summary["scope"].__setitem__(
                "image", "keystone"
            ),
            "scope_extra": lambda summary: summary["scope"].__setitem__(
                "unexpected", True
            ),
            "scope_count_type": lambda summary: summary["scope"].__setitem__(
                "image_count", float(summary["scope"]["image_count"])
            ),
        }
        for name, mutate in mutations.items():
            with self.subTest(field=name):
                summary = publish_summary()
                mutate(summary)

                result = run_validator(summary)

                self.assertEqual(result.returncode, 1)
                self.assertIn("publish summary", result.stderr)

        summary = publish_summary()
        del summary["registry"]
        result = run_validator(summary)
        self.assertEqual(result.returncode, 1)
        self.assertIn("publish summary registry must be", result.stderr)

    def test_summary_schema_rejects_missing_and_unexpected_keys(self) -> None:
        cases = {}

        unexpected_top_level = publish_summary()
        unexpected_top_level["environment"] = "dev"
        cases["unexpected top-level environment"] = unexpected_top_level

        missing_deploy_tag = publish_summary()
        missing_deploy_tag["images"][0].pop("deploy_tag")
        cases["missing required deploy_tag"] = missing_deploy_tag

        unexpected_image_key = publish_summary()
        unexpected_image_key["images"][0]["promotion_state"] = "candidate"
        cases["unexpected image key"] = unexpected_image_key

        unexpected_architecture_key = publish_summary()
        unexpected_architecture_key["images"][0]["architectures"][0][
            "runner"
        ] = "native-amd64"
        cases["unexpected architecture key"] = unexpected_architecture_key

        for name, summary in cases.items():
            with self.subTest(case=name):
                result = run_validator(summary)

                self.assertEqual(result.returncode, 1)
                self.assertIn("keys must be exactly", result.stderr)

    def test_duplicate_json_object_keys_are_rejected_at_every_level(self) -> None:
        for level, summary_json in duplicate_key_summary_json().items():
            with self.subTest(level=level):
                result = run_validator_json(summary_json)

                self.assertEqual(result.returncode, 2)
                self.assertIn("duplicate JSON object key", result.stderr)

    def test_leaf_evidence_mismatches_are_rejected(self) -> None:
        mutations = {
            "missing deploy_ref": lambda image: image.pop("deploy_ref"),
            "deploy_ref": lambda image: image.__setitem__(
                "deploy_ref", "ghcr.io/wrong/image:wrong"
            ),
            "deploy_tag": lambda image: image.__setitem__(
                "deploy_tag", "2025.1-rocky-9-amd64"
            ),
            "arch_ref": lambda image: image["architectures"][0].__setitem__(
                "arch_ref", "ghcr.io/wrong/image:wrong"
            ),
            "platform": lambda image: image["architectures"][0].__setitem__(
                "platform", "linux/arm64"
            ),
            "child_digest": lambda image: image["architectures"][0].__setitem__(
                "digest", "sha256:not-a-digest"
            ),
            "missing child_digest": lambda image: image["architectures"][0].pop(
                "digest"
            ),
            "manifest_digest": lambda image: image.__setitem__(
                "manifest_digest", "sha512:not-a-digest"
            ),
            "manifest_digest fallback": lambda image: (
                image.pop("manifest_digest"),
                image.__setitem__("digest", digest(999)),
                image.__setitem__(
                    "manifest_metadata",
                    {"containerimage.digest": digest(998)},
                ),
            ),
        }
        for name, mutate in mutations.items():
            with self.subTest(field=name):
                summary = publish_summary()
                mutate(summary["images"][0])

                result = run_validator(summary)

                self.assertEqual(result.returncode, 1)
                expected_error = (
                    "manifest_digest"
                    if name == "manifest_digest fallback"
                    else name.replace("missing ", "").replace("child_", "")
                )
                self.assertIn(expected_error, result.stderr)

    def test_variable_mappings_are_required_and_exact(self) -> None:
        mutations = {
            "missing": lambda image: image.pop("kolla_ansible_variables"),
            "extra": lambda image: image["kolla_ansible_variables"].append(
                "unexpected_image_full"
            ),
            "duplicate": lambda image: image["kolla_ansible_variables"].append(
                image["kolla_ansible_variables"][0]
            ),
        }
        for name, mutate in mutations.items():
            with self.subTest(case=name):
                summary = publish_summary("2025.2-rocky-10")
                mutate(image_entry(summary, "neutron-server"))

                result = run_validator(summary, stream="2025.2-rocky-10")

                self.assertEqual(result.returncode, 1)
                self.assertIn("kolla_ansible_variables do not match profile", result.stderr)

    def test_architecture_coverage_is_exact_and_unique(self) -> None:
        cases = []

        missing = publish_summary()
        missing["images"][0]["architectures"].pop()
        cases.append(("missing", missing, "architectures must be exactly"))

        extra = publish_summary()
        extra_arch = copy.deepcopy(extra["images"][0]["architectures"][0])
        extra_arch["arch"] = "s390x"
        extra_arch["platform"] = "linux/s390x"
        extra["images"][0]["architectures"].append(extra_arch)
        cases.append(("extra", extra, "architectures must be exactly"))

        duplicate = publish_summary()
        duplicate["images"][0]["architectures"].append(
            copy.deepcopy(duplicate["images"][0]["architectures"][0])
        )
        cases.append(("duplicate", duplicate, "duplicate architecture"))

        for name, summary, expected_error in cases:
            with self.subTest(case=name):
                result = run_validator(summary)

                self.assertEqual(result.returncode, 1)
                self.assertIn(expected_error, result.stderr)

    def test_full_core_summary_still_passes(self) -> None:
        result = run_validator(
            publish_summary(profile_name="core"),
            profile="core",
        )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_partial_core_non_keystone_summary_is_rejected(self) -> None:
        summary = publish_summary(
            profile_name="core",
            image_filter="glance-api",
        )

        result = run_validator(
            summary,
            profile="core",
            allow_partial=True,
            image="glance-api",
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn(
            "partial publish summaries are only supported for core/keystone",
            result.stderr,
        )

    def test_partial_deployment_summary_is_rejected(self) -> None:
        summary = publish_summary(
            profile_name="deployment",
            image_filter="nova-api",
        )

        result = run_validator(
            summary,
            profile="deployment",
            allow_partial=True,
            image="nova-api",
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn(
            "partial publish summaries are only supported for core/keystone",
            result.stderr,
        )

    def test_partial_core_keystone_requires_explicit_allow_and_image(self) -> None:
        summary = publish_summary(
            "2025.1-rocky-9",
            profile_name="core",
            image_filter="keystone",
        )

        without_partial = run_validator(summary, profile="core")
        self.assertEqual(without_partial.returncode, 1)
        self.assertIn("publish summary is missing image", without_partial.stderr)

        with_partial = run_validator(
            summary,
            profile="core",
            allow_partial=True,
            image="keystone",
        )
        self.assertEqual(with_partial.returncode, 0, with_partial.stderr)

        without_image = run_validator(summary, profile="core", allow_partial=True)
        self.assertEqual(without_image.returncode, 2)
        self.assertIn("--allow-partial requires --image", without_image.stderr)

        image_without_partial = run_validator(
            summary,
            profile="core",
            image="keystone",
        )
        self.assertEqual(image_without_partial.returncode, 2)
        self.assertIn("--image requires --allow-partial", image_without_partial.stderr)


if __name__ == "__main__":
    unittest.main()
