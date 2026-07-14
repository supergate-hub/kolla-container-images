from __future__ import annotations

import copy
import hashlib
import json
import re
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
GENERATE_LOCK = ROOT / "scripts" / "generate-lock.py"
PARSER_CONTRACT_PATH = (
    ROOT / "tests" / "fixtures" / "kolla-ansible-parse-image-contract.json"
)
PINNED_KOLLA_PARSER_MODULE_SHA256 = {
    "20.4.0": "3a22d2f70e8e3f3eea47be1b755ec5c37ed11d282e96db3094cd63846b01549f",
    "21.1.0": "1c4251075d6ee4987b8fc7bd0429064ef42c905a141f9c863c57d1a0b822d7a0",
    "22.0.0": "0cc53ffa96081cf6744bbe705652df381b3c4b4547728d01a471fbc0956ddfac",
}
ROOT_ASSIGNMENT_RE = re.compile(r'^([a-z0-9_]+): "([^"]+)"$')
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
NEW_NEUTRON_ALIASES = {
    "neutron_rpc_server_image_full",
    "neutron_periodic_worker_image_full",
    "neutron_ovn_maintenance_worker_image_full",
}
NEW_EXPORTER_ALIASES = {
    "prometheus_openstack_network_exporter_image_full",
    "prometheus_valkey_exporter_image_full",
}
STREAM_VARIABLE_COUNTS = {
    "2025.1-rocky-9": 65,
    "2025.1-rocky-10": 65,
    "2025.1-ubuntu-noble": 66,
    "2025.2-rocky-10": 68,
    "2025.2-ubuntu-noble": 69,
    "2026.1-rocky-10": 70,
    "2026.1-ubuntu-noble": 71,
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


def generate_lock_json(
    summary_json: str,
    *,
    stream: str = "2025.1-rocky-9",
    profile: str = "deployment",
    candidate_id: str = TEST_CANDIDATE_ID,
) -> tuple[subprocess.CompletedProcess[str], str | None]:
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        summary_path = temp_path / f"publish-summary-{stream}.json"
        output_path = temp_path / f"kolla-ansible-image-lock-{stream}.yml"
        summary_path.write_text(summary_json, encoding="utf-8")
        result = subprocess.run(
            [
                sys.executable,
                str(GENERATE_LOCK),
                "--publish-summary",
                str(summary_path),
                "--stream",
                stream,
                "--profile",
                profile,
                "--candidate-id",
                candidate_id,
                "--output",
                str(output_path),
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )
        output = output_path.read_text(encoding="utf-8") if output_path.exists() else None
        return result, output


def generate_lock(
    summary: dict,
    *,
    stream: str = "2025.1-rocky-9",
    profile: str = "deployment",
    candidate_id: str = TEST_CANDIDATE_ID,
) -> tuple[subprocess.CompletedProcess[str], str | None]:
    return generate_lock_json(
        json.dumps(summary),
        stream=stream,
        profile=profile,
        candidate_id=candidate_id,
    )


def lock_assignments(lock: str) -> list[tuple[str, str]]:
    assignments = []
    for line in lock.splitlines():
        match = ROOT_ASSIGNMENT_RE.fullmatch(line)
        if match:
            assignments.append((match.group(1), match.group(2)))
    return assignments


def parser_contract() -> dict:
    return json.loads(PARSER_CONTRACT_PATH.read_text(encoding="utf-8"))


def execute_pinned_parse_image(
    sources: dict[str, str], contract: dict[str, str], full_image: str
) -> list[str] | tuple[str, str]:
    source_digest = contract["parse_image_sha256"]
    source = sources[source_digest]
    if hashlib.sha256(source.encode()).hexdigest() != source_digest:
        raise AssertionError("pinned parse_image fixture digest mismatch")
    namespace = {"__builtins__": {}}
    exec(compile(source, contract["source_path"], "exec"), namespace)
    worker = type("PinnedWorker", (), {})()
    worker.params = {"image": full_image}
    return namespace["parse_image"](worker)


def parse_lock_yaml(lock: str) -> dict:
    lines = [
        line
        for line in lock.splitlines()
        if line and not line.startswith("#")
    ]
    index = 0

    def require(expected: str) -> None:
        nonlocal index
        if index >= len(lines) or lines[index] != expected:
            actual = lines[index] if index < len(lines) else "<end>"
            raise AssertionError(f"expected {expected!r}, got {actual!r}")
        index += 1

    def read_json(prefix: str):
        nonlocal index
        if index >= len(lines) or not lines[index].startswith(prefix):
            actual = lines[index] if index < len(lines) else "<end>"
            raise AssertionError(f"expected prefix {prefix!r}, got {actual!r}")
        value = json.loads(lines[index][len(prefix):])
        index += 1
        return value

    require("_kolla_candidate_lock:")
    require("  schema_version: 1")
    stream = read_json("  stream: ")
    require("  scope:")
    require('    profile: "deployment"')
    require('    image: "all"')
    image_count = int(read_json("    image_count: "))
    require("  images:")

    images = {}
    while index < len(lines) and lines[index].startswith('    "'):
        image = json.loads(lines[index][4:-1])
        if image in images:
            raise AssertionError(f"duplicate metadata image: {image}")
        index += 1
        deploy_ref = read_json("      deploy_ref: ")
        manifest_digest = read_json("      manifest_digest: ")
        immutable_ref = read_json("      immutable_ref: ")
        require("      kolla_ansible_variables:")
        variables = []
        while index < len(lines) and lines[index].startswith("        - "):
            variables.append(json.loads(lines[index][10:]))
            index += 1
        images[image] = {
            "deploy_ref": deploy_ref,
            "manifest_digest": manifest_digest,
            "immutable_ref": immutable_ref,
            "kolla_ansible_variables": variables,
        }

    parsed = {
        "_kolla_candidate_lock": {
            "schema_version": 1,
            "stream": stream,
            "scope": {
                "profile": "deployment",
                "image": "all",
                "image_count": image_count,
            },
            "images": images,
        }
    }
    while index < len(lines):
        match = ROOT_ASSIGNMENT_RE.fullmatch(lines[index])
        if not match or match.group(1) in parsed:
            raise AssertionError(f"invalid or duplicate root assignment: {lines[index]}")
        parsed[match.group(1)] = match.group(2)
        index += 1
    return parsed


def expected_lock_data(stream_id: str, summary: dict) -> dict:
    stream, profile = resolved_profile(stream_id, "deployment")
    summaries = {image["image"]: image for image in summary["images"]}
    metadata_images = {}
    assignments = {}
    for profile_image in profile["images"]:
        entry = summaries[profile_image["name"]]
        repository, _deploy_tag = entry["deploy_ref"].rsplit(":", 1)
        variables = profile_image["kolla_ansible_variables"]
        metadata_images[profile_image["name"]] = {
            "deploy_ref": entry["deploy_ref"],
            "manifest_digest": entry["manifest_digest"],
            "immutable_ref": f'{repository}@{entry["manifest_digest"]}',
            "kolla_ansible_variables": variables,
        }
        for variable in variables:
            assignments[variable] = entry["deploy_ref"]
    return {
        "_kolla_candidate_lock": {
            "schema_version": 1,
            "stream": stream["id"],
            "scope": {
                "profile": "deployment",
                "image": "all",
                "image_count": len(profile["images"]),
            },
            "images": metadata_images,
        },
        **assignments,
    }


def expected_assignments(stream_id: str, summary: dict) -> dict[str, str]:
    expected = expected_lock_data(stream_id, summary)
    return {
        key: value
        for key, value in expected.items()
        if key != "_kolla_candidate_lock"
    }


class LockGenerationTest(unittest.TestCase):
    def test_candidate_lock_root_and_metadata_use_candidate_ref(self) -> None:
        summary = publish_summary()
        result, lock = generate_lock(summary)
        self.assertEqual(result.returncode, 0, result.stderr)
        assert lock is not None
        parsed = parse_lock_yaml(lock)
        entry = parsed["_kolla_candidate_lock"]["images"]["keystone"]
        self.assertEqual(entry["deploy_ref"], candidate_ref)
        self.assertEqual(parsed["keystone_image_full"], candidate_ref)
        self.assertEqual(
            entry["immutable_ref"],
            "ghcr.io/supergate-hub/kolla-container-images/keystone@"
            + entry["manifest_digest"],
        )

    def test_lock_candidate_id_must_match_expected_id(self) -> None:
        summary = publish_summary()
        summary["candidate_id"] = "123456789-2"
        result, lock = generate_lock(summary)
        self.assertEqual(result.returncode, 2)
        self.assertIsNone(lock)
        self.assertIn("candidate ID", result.stderr)

    def test_lock_malformed_expected_candidate_id_is_rejected(self) -> None:
        result, lock = generate_lock(
            publish_summary(),
            candidate_id="01-1",
        )
        self.assertEqual(result.returncode, 2)
        self.assertIsNone(lock)
        self.assertIn("candidate ID", result.stderr)

    def test_complete_deployment_writes_every_resolved_variable_once(self) -> None:
        for stream_id, expected_count in STREAM_VARIABLE_COUNTS.items():
            with self.subTest(stream=stream_id):
                summary = publish_summary(stream_id)
                result, lock = generate_lock(summary, stream=stream_id)

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIsNotNone(lock)
                assert lock is not None
                assignments = lock_assignments(lock)
                variables = [variable for variable, _ in assignments]
                self.assertEqual(len(assignments), expected_count)
                self.assertEqual(len(variables), len(set(variables)))
                self.assertEqual(dict(assignments), expected_assignments(stream_id, summary))
                for variable, value in assignments:
                    self.assertRegex(variable, r"^[a-z0-9_]+$")
                    self.assertNotIn("@", value)
                    self.assertNotIn("-amd64", value)
                    self.assertNotIn("-arm64", value)

                for forbidden_field in (
                    "environment:",
                    "promotion_state:",
                    "pointer:",
                    "inventory:",
                    "deployment_action:",
                ):
                    self.assertNotIn(forbidden_field, lock)

    def test_tag_digest_value_is_incompatible_with_pinned_kolla_parser(self) -> None:
        fixture = parser_contract()
        self.assertEqual(fixture["schema_version"], 1)
        contracts = fixture["versions"]
        versions = {
            stream["kolla_ansible_version"] for stream in MATRIX["streams"]
        }
        self.assertEqual(set(contracts), versions)
        self.assertEqual(
            {
                version: contract["module_sha256"]
                for version, contract in contracts.items()
            },
            PINNED_KOLLA_PARSER_MODULE_SHA256,
        )

        entry = publish_summary()["images"][0]
        legacy_ref = f'{entry["deploy_ref"]}@{entry["manifest_digest"]}'
        expected_digest_hex = entry["manifest_digest"].removeprefix("sha256:")
        for version, contract in contracts.items():
            with self.subTest(kolla_ansible_version=version):
                image, tag = execute_pinned_parse_image(
                    fixture["sources"], contract, legacy_ref
                )
                self.assertEqual(image, f'{entry["deploy_ref"]}@sha256')
                self.assertEqual(tag, expected_digest_hex)

    def test_generated_lock_structurally_matches_summary_and_pinned_parser(self) -> None:
        fixture = parser_contract()
        contracts = fixture["versions"]
        for stream_id in STREAM_VARIABLE_COUNTS:
            with self.subTest(stream=stream_id):
                stream, _profile = resolved_profile(stream_id, "deployment")
                summary = publish_summary(stream_id)
                result, lock = generate_lock(summary, stream=stream_id)

                self.assertEqual(result.returncode, 0, result.stderr)
                assert lock is not None
                parsed = parse_lock_yaml(lock)
                self.assertEqual(parsed, expected_lock_data(stream_id, summary))

                contract = contracts[stream["kolla_ansible_version"]]
                for entry in summary["images"]:
                    expected_image, expected_tag = entry["deploy_ref"].rsplit(":", 1)
                    for variable in entry["kolla_ansible_variables"]:
                        value = parsed[variable]
                        self.assertNotIn("@", value)
                        self.assertEqual(
                            execute_pinned_parse_image(
                                fixture["sources"], contract, value
                            ),
                            [expected_image, expected_tag],
                        )

    def test_resolved_conditional_aliases_are_stream_specific(self) -> None:
        cases = {
            "2025.1-rocky-9": (
                set(),
                NEW_NEUTRON_ALIASES
                | NEW_EXPORTER_ALIASES
                | {"tgtd_image_full"},
            ),
            "2025.1-rocky-10": (
                set(),
                NEW_NEUTRON_ALIASES
                | NEW_EXPORTER_ALIASES
                | {"tgtd_image_full"},
            ),
            "2025.1-ubuntu-noble": (
                {"tgtd_image_full"},
                NEW_NEUTRON_ALIASES | NEW_EXPORTER_ALIASES,
            ),
            "2025.2-rocky-10": (
                NEW_NEUTRON_ALIASES,
                NEW_EXPORTER_ALIASES | {"tgtd_image_full"},
            ),
            "2025.2-ubuntu-noble": (
                NEW_NEUTRON_ALIASES | {"tgtd_image_full"},
                NEW_EXPORTER_ALIASES,
            ),
            "2026.1-rocky-10": (
                NEW_NEUTRON_ALIASES | NEW_EXPORTER_ALIASES,
                {"tgtd_image_full"},
            ),
            "2026.1-ubuntu-noble": (
                NEW_NEUTRON_ALIASES
                | NEW_EXPORTER_ALIASES
                | {"tgtd_image_full"},
                set(),
            ),
        }
        for stream_id, (expected_present, expected_absent) in cases.items():
            with self.subTest(stream=stream_id):
                result, lock = generate_lock(
                    publish_summary(stream_id),
                    stream=stream_id,
                )

                self.assertEqual(result.returncode, 0, result.stderr)
                assert lock is not None
                variables = {variable for variable, _ in lock_assignments(lock)}
                self.assertTrue(expected_present <= variables)
                self.assertTrue(expected_absent.isdisjoint(variables))

    def test_non_deployment_profile_is_rejected(self) -> None:
        summary = publish_summary(profile_name="core")

        result, lock = generate_lock(summary, profile="core")

        self.assertEqual(result.returncode, 2)
        self.assertIn("candidate lock requires profile 'deployment'", result.stderr)
        self.assertIsNone(lock)

    def test_partial_deployment_scope_is_rejected(self) -> None:
        summary = publish_summary(image_filter="keystone")

        result, lock = generate_lock(summary)

        self.assertEqual(result.returncode, 2)
        self.assertIn("deployment/all", result.stderr)
        self.assertIsNone(lock)

    def test_missing_extra_and_duplicate_images_are_rejected(self) -> None:
        stream, _ = resolved_profile("2025.1-rocky-9", "deployment")
        cases = []

        missing = publish_summary()
        missing["images"].pop()
        cases.append(("missing", missing, "missing image"))

        extra = publish_summary()
        extra["images"].append(
            summary_image(
                stream,
                {"name": "base", "kolla_ansible_variables": []},
                100,
            )
        )
        cases.append(("extra", extra, "unexpected image: base"))

        duplicate = publish_summary()
        duplicate["images"].append(copy.deepcopy(duplicate["images"][0]))
        cases.append(("duplicate", duplicate, "duplicate image"))

        substitution = publish_summary()
        substitution["images"].pop()
        substitution["images"].append(
            summary_image(
                stream,
                {"name": "base", "kolla_ansible_variables": []},
                100,
            )
        )
        cases.append(("same-count substitution", substitution, "missing image"))

        for name, summary, expected_error in cases:
            with self.subTest(case=name):
                result, lock = generate_lock(summary)

                self.assertEqual(result.returncode, 2)
                self.assertIn(expected_error, result.stderr)
                self.assertIsNone(lock)

    def test_scope_mismatches_are_rejected(self) -> None:
        mutations = {
            "profile": lambda scope: scope.__setitem__("profile", "core"),
            "image": lambda scope: scope.__setitem__("image", "keystone"),
            "count": lambda scope: scope.__setitem__(
                "image_count", scope["image_count"] - 1
            ),
            "extra": lambda scope: scope.__setitem__("unexpected", True),
            "count_type": lambda scope: scope.__setitem__(
                "image_count", float(scope["image_count"])
            ),
        }
        for name, mutate in mutations.items():
            with self.subTest(field=name):
                summary = publish_summary()
                mutate(summary["scope"])

                result, lock = generate_lock(summary)

                self.assertEqual(result.returncode, 2)
                self.assertIn("deployment/all", result.stderr)
                self.assertIsNone(lock)

    def test_mismatched_summary_evidence_is_rejected(self) -> None:
        mutations = {
            "owner": (
                lambda summary: summary.__setitem__("owner", "wrong-owner"),
                "owner",
            ),
            "missing deploy_ref": (
                lambda summary: summary["images"][0].pop("deploy_ref"),
                "deploy_ref",
            ),
            "deploy_ref": (
                lambda summary: summary["images"][0].__setitem__(
                    "deploy_ref", "ghcr.io/wrong/image:wrong"
                ),
                "deploy_ref",
            ),
            "variables": (
                lambda summary: summary["images"][0].pop(
                    "kolla_ansible_variables"
                ),
                "kolla_ansible_variables",
            ),
            "child_digest": (
                lambda summary: summary["images"][0]["architectures"][
                    0
                ].__setitem__("digest", "sha256:bad"),
                "digest",
            ),
            "manifest_digest": (
                lambda summary: summary["images"][0].__setitem__(
                    "manifest_digest", "sha256:bad"
                ),
                "manifest_digest",
            ),
            "manifest fallback": (
                lambda summary: (
                    summary["images"][0].pop("manifest_digest"),
                    summary["images"][0].__setitem__("digest", digest(999)),
                    summary["images"][0].__setitem__(
                        "manifest_metadata",
                        {"containerimage.digest": digest(998)},
                    ),
                ),
                "manifest_digest",
            ),
        }
        for name, (mutate, expected_error) in mutations.items():
            with self.subTest(field=name):
                summary = publish_summary()
                mutate(summary)

                result, lock = generate_lock(summary)

                self.assertEqual(result.returncode, 2)
                self.assertIn(expected_error, result.stderr)
                self.assertIsNone(lock)

    def test_malformed_summary_schema_does_not_generate_lock(self) -> None:
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
                result, lock = generate_lock(summary)

                self.assertEqual(result.returncode, 2)
                self.assertIn("keys must be exactly", result.stderr)
                self.assertIsNone(lock)

    def test_duplicate_json_object_keys_do_not_generate_lock(self) -> None:
        for level, summary_json in duplicate_key_summary_json().items():
            with self.subTest(level=level):
                result, lock = generate_lock_json(summary_json)

                self.assertEqual(result.returncode, 2)
                self.assertIn("duplicate JSON object key", result.stderr)
                self.assertIsNone(lock)


if __name__ == "__main__":
    unittest.main()
