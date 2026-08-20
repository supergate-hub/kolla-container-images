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

from scripts.base_resolution import resolve_base
from scripts.openstack_source_set import render_frozen_configs
from scripts.profile_resolver import (
    find_stream,
    load_matrix,
    load_profile,
    render_tag,
    render_revision_tag,
    resolve_profile,
)


ROOT = Path(__file__).resolve().parents[1]
GENERATE_LOCK = ROOT / "scripts" / "generate-lock.py"
PARSER_CONTRACT_PATH = (
    ROOT / "tests" / "fixtures" / "kolla-ansible-parse-image-contract.json"
)
PINNED_KOLLA_PARSER_MODULE_SHA256 = {
    "20.4.0": "3a22d2f70e8e3f3eea47be1b755ec5c37ed11d282e96db3094cd63846b01549f",
    "20.5.0": "3a22d2f70e8e3f3eea47be1b755ec5c37ed11d282e96db3094cd63846b01549f",
    "21.2.0": "1c4251075d6ee4987b8fc7bd0429064ef42c905a141f9c863c57d1a0b822d7a0",
    "22.1.0": "0cc53ffa96081cf6744bbe705652df381b3c4b4547728d01a471fbc0956ddfac",
}
PARSER_CONTRACT_ALIASES = {"20.5.0": "20.4.0"}
ROOT_ASSIGNMENT_RE = re.compile(r'^([a-z0-9_]+): "([^"]+)"$')
MATRIX = load_matrix()
TEST_CANDIDATE_ID = "123456789-1"
STREAM_IDS = [stream["id"] for stream in MATRIX["streams"]]
DEFAULT_STREAM = STREAM_IDS[0]
SOURCE_SET_DIR = ROOT / "config" / "openstack-sources"
BASE_INDEX_FIXTURE = ROOT / "tests" / "fixtures" / "oci-base-index.json"
NEW_NEUTRON_ALIASES = {
    "neutron_rpc_server_image_full",
    "neutron_periodic_worker_image_full",
    "neutron_ovn_maintenance_worker_image_full",
}
NEW_EXPORTER_ALIASES = {
    "prometheus_openstack_network_exporter_image_full",
    "prometheus_valkey_exporter_image_full",
}
def digest(index: int) -> str:
    return f"sha256:{index:064x}"


def resolved_profile(stream_id: str, profile_name: str) -> tuple[dict, dict]:
    stream = find_stream(MATRIX, stream_id)
    profile = resolve_profile(load_profile(profile_name), stream)
    return stream, profile


def openstack_sources(stream: dict) -> dict:
    document = json.loads(
        (SOURCE_SET_DIR / f"{stream['source_set_id']}.json").read_text(encoding="utf-8")
    )
    canonical = json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    rendered = render_frozen_configs(document)
    return {
        "source_set": document,
        "canonical_digest": f"sha256:{hashlib.sha256(canonical).hexdigest()}",
        "kolla_build_config_sha256": rendered.config_sha256,
        "template_override_sha256": rendered.template_override_sha256,
    }


def frozen_base(stream: dict) -> dict:
    return resolve_base(
        {
            "id": stream["base_id"],
            "distro": stream["distro"],
            "os_version": stream["os_version"],
            "image": stream["base_image"],
            "tag": stream["base_tag"],
        },
        BASE_INDEX_FIXTURE.read_bytes(),
    )


def summary_image(stream: dict, profile_image: dict, index: int) -> dict:
    image = profile_image["name"]
    semantic_tag = render_tag(MATRIX, stream)
    revision_tag = render_revision_tag(MATRIX, stream, TEST_CANDIDATE_ID)
    repository = (
        f"{MATRIX['registry']}/{MATRIX['owner']}/{MATRIX['repository']}/{image}"
    )
    manifest_digest = digest(index * 10 + 9)
    return {
        "image": image,
        "kolla_ansible_variables": profile_image["kolla_ansible_variables"],
        "semantic_tag": semantic_tag,
        "semantic_ref": f"{repository}:{semantic_tag}",
        "revision_tag": revision_tag,
        "revision_ref": f"{repository}:{revision_tag}",
        "manifest_digest": manifest_digest,
        "immutable_ref": f"{repository}@{manifest_digest}",
        "architectures": [
            {
                "arch": arch,
                "platform": f"linux/{arch}",
                "revision_arch_ref": f"{repository}:"
                f"{render_revision_tag(MATRIX, stream, TEST_CANDIDATE_ID, arch)}",
                "digest": digest(index * 10 + arch_index + 1),
            }
            for arch_index, arch in enumerate(MATRIX["architectures"])
        ],
    }


def publish_summary(
    stream_id: str = DEFAULT_STREAM,
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
        "schema_version": 3,
        "candidate_id": TEST_CANDIDATE_ID,
        "stream": stream["id"],
        "release": stream["release"],
        "release_series": stream["release_series"],
        "release_branch": stream["release_branch"],
        "distro": stream["distro"],
        "distro_version": stream["base_tag"],
        "base": frozen_base(stream),
        "openstack_sources": openstack_sources(stream),
        "release_metadata": copy.deepcopy(MATRIX["release_metadata"]),
        "kolla": {
            "repository": stream["kolla_repository"],
            "version": stream["kolla_version"],
            "commit": stream["kolla_commit"],
        },
        "kolla_ansible": {
            "repository": stream["kolla_ansible_repository"],
            "version": stream["kolla_ansible_version"],
            "commit": stream["kolla_ansible_commit"],
        },
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
            '"candidate_id": ',
            '"candidate_id": "ignored", "candidate_id": ',
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
    stream: str = DEFAULT_STREAM,
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
    stream: str = DEFAULT_STREAM,
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


def parser_contract_for(contracts: dict, version: str) -> dict:
    return contracts[PARSER_CONTRACT_ALIASES.get(version, version)]


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
    require("  schema_version: 3")
    candidate_id = read_json("  candidate_id: ")
    stream = read_json("  stream: ")
    release = read_json("  release: ")
    release_series = read_json("  release_series: ")
    release_branch = read_json("  release_branch: ")
    require("  release_metadata:")
    release_metadata = {
        "repository": read_json("    repository: "),
        "commit": read_json("    commit: "),
    }
    require("  kolla:")
    kolla = {
        "repository": read_json("    repository: "),
        "version": read_json("    version: "),
        "commit": read_json("    commit: "),
    }
    require("  kolla_ansible:")
    kolla_ansible = {
        "repository": read_json("    repository: "),
        "version": read_json("    version: "),
        "commit": read_json("    commit: "),
    }
    require("  base:")
    base = {
        "id": read_json("    id: "),
        "requested_ref": read_json("    requested_ref: "),
        "index_digest": read_json("    index_digest: "),
        "index_manifest_b64": read_json("    index_manifest_b64: "),
        "platforms": read_json("    platforms: "),
    }
    require("  openstack_sources:")
    openstack_sources = {
        "source_set": read_json("    source_set: "),
        "canonical_digest": read_json("    canonical_digest: "),
        "kolla_build_config_sha256": read_json(
            "    kolla_build_config_sha256: "
        ),
        "template_override_sha256": read_json(
            "    template_override_sha256: "
        ),
    }
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
        semantic_ref = read_json("      semantic_ref: ")
        revision_ref = read_json("      revision_ref: ")
        manifest_digest = read_json("      manifest_digest: ")
        immutable_ref = read_json("      immutable_ref: ")
        architectures = read_json("      architectures: ")
        require("      kolla_ansible_variables:")
        variables = []
        while index < len(lines) and lines[index].startswith("        - "):
            variables.append(json.loads(lines[index][10:]))
            index += 1
        images[image] = {
            "semantic_ref": semantic_ref,
            "revision_ref": revision_ref,
            "manifest_digest": manifest_digest,
            "immutable_ref": immutable_ref,
            "architectures": architectures,
            "kolla_ansible_variables": variables,
        }

    parsed = {
        "_kolla_candidate_lock": {
            "schema_version": 3,
            "candidate_id": candidate_id,
            "stream": stream,
            "release": release,
            "release_series": release_series,
            "release_branch": release_branch,
            "release_metadata": release_metadata,
            "kolla": kolla,
            "kolla_ansible": kolla_ansible,
            "base": base,
            "openstack_sources": openstack_sources,
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
        variables = profile_image["kolla_ansible_variables"]
        metadata_images[profile_image["name"]] = {
            "semantic_ref": entry["semantic_ref"],
            "revision_ref": entry["revision_ref"],
            "manifest_digest": entry["manifest_digest"],
            "immutable_ref": entry["immutable_ref"],
            "architectures": entry["architectures"],
            "kolla_ansible_variables": variables,
        }
        for variable in variables:
            assignments[variable] = entry["revision_ref"]
    return {
        "_kolla_candidate_lock": {
            "schema_version": 3,
            "candidate_id": summary["candidate_id"],
            "stream": stream["id"],
            "release": summary["release"],
            "release_series": summary["release_series"],
            "release_branch": summary["release_branch"],
            "release_metadata": summary["release_metadata"],
            "kolla": summary["kolla"],
            "kolla_ansible": summary["kolla_ansible"],
            "base": summary["base"],
            "openstack_sources": summary["openstack_sources"],
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
    def test_candidate_lock_root_uses_revision_ref_and_preserves_semantic_ref(self) -> None:
        summary = publish_summary()
        result, lock = generate_lock(summary)
        self.assertEqual(result.returncode, 0, result.stderr)
        assert lock is not None
        parsed = parse_lock_yaml(lock)
        metadata = parsed["_kolla_candidate_lock"]
        self.assertEqual(metadata["schema_version"], 3)
        self.assertEqual(metadata["candidate_id"], TEST_CANDIDATE_ID)
        self.assertEqual(metadata["release"], summary["release"])
        self.assertEqual(metadata["release_series"], summary["release_series"])
        self.assertEqual(metadata["release_branch"], summary["release_branch"])
        self.assertEqual(metadata["release_metadata"], summary["release_metadata"])
        self.assertEqual(metadata["kolla"], summary["kolla"])
        self.assertEqual(metadata["kolla_ansible"], summary["kolla_ansible"])
        self.assertEqual(metadata["base"], summary["base"])
        self.assertEqual(metadata["openstack_sources"], summary["openstack_sources"])
        entry = metadata["images"]["keystone"]
        summary_entry = image_entry(summary, "keystone")
        semantic_ref = summary_entry["semantic_ref"]
        revision_ref = summary_entry["revision_ref"]
        self.assertEqual(entry["semantic_ref"], semantic_ref)
        self.assertEqual(entry["revision_ref"], revision_ref)
        self.assertNotEqual(semantic_ref, revision_ref)
        self.assertEqual(parsed["keystone_image_full"], revision_ref)
        self.assertEqual(
            [
                architecture["revision_arch_ref"]
                for architecture in summary_entry["architectures"]
            ],
            [f"{revision_ref}-amd64", f"{revision_ref}-arm64"],
        )
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
        for stream_id in STREAM_IDS:
            with self.subTest(stream=stream_id):
                summary = publish_summary(stream_id)
                result, lock = generate_lock(summary, stream=stream_id)

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIsNotNone(lock)
                assert lock is not None
                assignments = lock_assignments(lock)
                variables = [variable for variable, _ in assignments]
                expected_count = len(expected_assignments(stream_id, summary))
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
            find_stream(MATRIX, stream["id"])["kolla_ansible_version"]
            for stream in MATRIX["streams"]
        }
        self.assertTrue(versions <= set(PINNED_KOLLA_PARSER_MODULE_SHA256))
        self.assertEqual(
            {
                version: parser_contract_for(contracts, version)["module_sha256"]
                for version in versions
            },
            {
                version: PINNED_KOLLA_PARSER_MODULE_SHA256[version]
                for version in versions
            },
        )

        entry = publish_summary()["images"][0]
        legacy_ref = f'{entry["revision_ref"]}@{entry["manifest_digest"]}'
        expected_digest_hex = entry["manifest_digest"].removeprefix("sha256:")
        for version, contract in contracts.items():
            with self.subTest(kolla_ansible_version=version):
                image, tag = execute_pinned_parse_image(
                    fixture["sources"], contract, legacy_ref
                )
                self.assertEqual(image, f'{entry["revision_ref"]}@sha256')
                self.assertEqual(tag, expected_digest_hex)

    def test_generated_lock_structurally_matches_summary_and_pinned_parser(self) -> None:
        fixture = parser_contract()
        contracts = fixture["versions"]
        for stream_id in STREAM_IDS:
            with self.subTest(stream=stream_id):
                stream, _profile = resolved_profile(stream_id, "deployment")
                summary = publish_summary(stream_id)
                result, lock = generate_lock(summary, stream=stream_id)

                self.assertEqual(result.returncode, 0, result.stderr)
                assert lock is not None
                parsed = parse_lock_yaml(lock)
                self.assertEqual(parsed, expected_lock_data(stream_id, summary))

                contract = parser_contract_for(
                    contracts, stream["kolla_ansible_version"]
                )
                for entry in summary["images"]:
                    expected_image, expected_tag = entry["revision_ref"].rsplit(":", 1)
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
            "2025.1-rocky-9.8-20.4.0": (
                set(),
                NEW_NEUTRON_ALIASES
                | NEW_EXPORTER_ALIASES
                | {"tgtd_image_full"},
            ),
            "2025.1-rocky-10.2-20.4.0": (
                set(),
                NEW_NEUTRON_ALIASES
                | NEW_EXPORTER_ALIASES
                | {"tgtd_image_full"},
            ),
            "2025.1-ubuntu-24.04-20.4.0": (
                {"tgtd_image_full"},
                NEW_NEUTRON_ALIASES | NEW_EXPORTER_ALIASES,
            ),
            "2025.2-rocky-10.2-21.2.0": (
                NEW_NEUTRON_ALIASES,
                NEW_EXPORTER_ALIASES | {"tgtd_image_full"},
            ),
            "2025.2-ubuntu-24.04-21.2.0": (
                NEW_NEUTRON_ALIASES | {"tgtd_image_full"},
                NEW_EXPORTER_ALIASES,
            ),
            "2026.1-rocky-10.2-22.1.0": (
                NEW_NEUTRON_ALIASES | NEW_EXPORTER_ALIASES,
                {"tgtd_image_full"},
            ),
            "2026.1-ubuntu-24.04-22.1.0": (
                NEW_NEUTRON_ALIASES
                | NEW_EXPORTER_ALIASES
                | {"tgtd_image_full"},
                set(),
            ),
        }
        for stream_id, (expected_present, expected_absent) in cases.items():
            if stream_id not in STREAM_IDS:
                continue
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
        stream, _ = resolved_profile(DEFAULT_STREAM, "deployment")
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
            "release_series": (
                lambda summary: summary.__setitem__("release_series", "wrong-series"),
                "release_series",
            ),
            "release_branch": (
                lambda summary: summary.__setitem__("release_branch", "wrong-branch"),
                "release_branch",
            ),
            "release_metadata": (
                lambda summary: summary["release_metadata"].__setitem__(
                    "commit", "0" * 40
                ),
                "release_metadata",
            ),
            "kolla": (
                lambda summary: summary["kolla"].__setitem__("commit", "0" * 40),
                "kolla",
            ),
            "kolla_ansible": (
                lambda summary: summary["kolla_ansible"].__setitem__(
                    "commit", "0" * 40
                ),
                "kolla_ansible",
            ),
            "base": (
                lambda summary: summary["base"].__setitem__(
                    "requested_ref", "registry.invalid/base:wrong"
                ),
                "base",
            ),
            "openstack source set": (
                lambda summary: summary["openstack_sources"]["source_set"].__setitem__(
                    "id", "wrong-source-set"
                ),
                "source_set",
            ),
            "missing revision_ref": (
                lambda summary: summary["images"][0].pop("revision_ref"),
                "revision_ref",
            ),
            "revision_ref": (
                lambda summary: summary["images"][0].__setitem__(
                    "revision_ref", "ghcr.io/wrong/image:wrong"
                ),
                "revision_ref",
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

        missing_release_metadata = publish_summary()
        missing_release_metadata.pop("release_metadata")
        cases["missing release_metadata"] = missing_release_metadata

        missing_revision_tag = publish_summary()
        missing_revision_tag["images"][0].pop("revision_tag")
        cases["missing required revision_tag"] = missing_revision_tag

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
