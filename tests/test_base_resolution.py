from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from scripts.base_resolution import (
    BaseResolutionError,
    resolve_base,
    validate_resolved_base,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


class BaseResolutionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.base = {
            "id": "rocky-10.2",
            "distro": "rocky",
            "os_version": "10.2",
            "image": "quay.io/rockylinux/rockylinux",
            "tag": "10.2",
        }
        self.raw_manifest = (FIXTURES / "oci-base-index.json").read_bytes()

    def encode_index(self, **changes: object) -> bytes:
        index = json.loads(self.raw_manifest)
        index.update(changes)
        return json.dumps(index).encode("utf-8")

    def test_resolves_exact_required_platforms_from_raw_oci_index(self) -> None:
        resolved = resolve_base(
            self.base,
            self.raw_manifest,
        )

        self.assertEqual(resolved["id"], "rocky-10.2")
        self.assertEqual(
            resolved["requested_ref"],
            "quay.io/rockylinux/rockylinux:10.2",
        )
        self.assertEqual(
            resolved["index_digest"],
            f"sha256:{hashlib.sha256(self.raw_manifest).hexdigest()}",
        )
        self.assertEqual(
            resolved["index_manifest_b64"],
            base64.b64encode(self.raw_manifest).decode("ascii"),
        )
        self.assertEqual(
            resolved["platforms"],
            {
                "amd64": {
                    "platform": "linux/amd64",
                    "digest": "sha256:" + "1" * 64,
                },
                "arm64": {
                    "platform": "linux/arm64",
                    "digest": "sha256:" + "2" * 64,
                },
            },
        )

    def test_rejects_digest_pins_in_configured_base(self) -> None:
        for field, value in (
            ("digest", "sha256:" + "a" * 64),
            ("index_digest", "sha256:" + "b" * 64),
            ("platform_digests", {"amd64": "sha256:" + "c" * 64}),
        ):
            with self.subTest(field=field):
                base = dict(self.base)
                base[field] = value
                with self.assertRaisesRegex(
                    BaseResolutionError,
                    "must not contain digest",
                ):
                    resolve_base(base, self.raw_manifest)

        base = dict(self.base)
        base["image"] = "quay.io/rockylinux/rockylinux@sha256:" + "d" * 64
        with self.assertRaisesRegex(
            BaseResolutionError,
            "must not contain a digest",
        ):
            resolve_base(base, self.raw_manifest)

    def test_accepts_a_manifest_adapter_at_the_external_lookup_seam(self) -> None:
        inspected_refs: list[str] = []

        def inspect(requested_ref: str) -> bytes:
            inspected_refs.append(requested_ref)
            return self.raw_manifest

        resolved = resolve_base(self.base, inspect)

        self.assertEqual(
            inspected_refs,
            ["quay.io/rockylinux/rockylinux:10.2"],
        )
        self.assertEqual(
            resolved["index_digest"],
            f"sha256:{hashlib.sha256(self.raw_manifest).hexdigest()}",
        )

    def test_default_adapter_inspects_the_requested_tag_as_raw_bytes(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=self.raw_manifest,
            stderr=b"",
        )
        with mock.patch(
            "scripts.base_resolution.subprocess.run",
            return_value=completed,
        ) as run:
            resolved = resolve_base(self.base)

        run.assert_called_once_with(
            [
                "docker",
                "buildx",
                "imagetools",
                "inspect",
                "--raw",
                "quay.io/rockylinux/rockylinux:10.2",
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(
            resolved["index_digest"],
            f"sha256:{hashlib.sha256(self.raw_manifest).hexdigest()}",
        )

    def test_default_adapter_reports_lookup_failures_as_resolution_errors(self) -> None:
        failures = (
            FileNotFoundError("docker is not installed"),
            subprocess.CalledProcessError(
                1,
                ["docker"],
                stderr=b"registry denied the request",
            ),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__), mock.patch(
                "scripts.base_resolution.subprocess.run",
                side_effect=failure,
            ), self.assertRaisesRegex(
                BaseResolutionError,
                "cannot inspect base manifest",
            ):
                resolve_base(self.base)

    def test_expected_index_digest_must_match_the_exact_raw_bytes(self) -> None:
        actual_digest = f"sha256:{hashlib.sha256(self.raw_manifest).hexdigest()}"

        resolved = resolve_base(
            self.base,
            self.raw_manifest,
            expected_digest=actual_digest,
        )
        self.assertEqual(resolved["index_digest"], actual_digest)

        with self.assertRaisesRegex(BaseResolutionError, "digest mismatch"):
            resolve_base(
                self.base,
                self.raw_manifest,
                expected_digest="sha256:" + "0" * 64,
            )

        for malformed in ("sha256:abc", "sha512:" + "0" * 64, "SHA256:" + "0" * 64):
            with self.subTest(malformed=malformed), self.assertRaisesRegex(
                BaseResolutionError,
                "expected digest",
            ):
                resolve_base(
                    self.base,
                    self.raw_manifest,
                    expected_digest=malformed,
                )

    def test_resolves_a_docker_v2_manifest_list(self) -> None:
        raw_manifest = (FIXTURES / "docker-base-index.json").read_bytes()

        resolved = resolve_base(self.base, raw_manifest)

        self.assertEqual(
            resolved["platforms"],
            {
                "amd64": {
                    "platform": "linux/amd64",
                    "digest": "sha256:" + "a" * 64,
                },
                "arm64": {
                    "platform": "linux/arm64",
                    "digest": "sha256:" + "b" * 64,
                },
            },
        )

    def test_rejects_a_single_image_manifest_instead_of_an_index(self) -> None:
        manifest = json.loads(self.raw_manifest)
        manifest["mediaType"] = "application/vnd.oci.image.manifest.v1+json"
        raw_manifest = json.dumps(manifest).encode("utf-8")

        with self.assertRaisesRegex(
            BaseResolutionError,
            "unsupported index mediaType",
        ):
            resolve_base(self.base, raw_manifest)

    def test_raw_manifest_must_be_a_json_object(self) -> None:
        for name, raw_manifest in (
            ("invalid JSON", b"not-json"),
            ("JSON array", b"[]"),
        ):
            with self.subTest(name=name), self.assertRaisesRegex(
                BaseResolutionError,
                "raw manifest must be a JSON object",
            ):
                resolve_base(self.base, raw_manifest)

    def test_raw_manifest_rejects_duplicate_json_keys(self) -> None:
        raw_manifest = self.raw_manifest.replace(
            b'"schemaVersion":2,',
            b'"schemaVersion":2,"schemaVersion":2,',
            1,
        )

        with self.assertRaisesRegex(
            BaseResolutionError,
            "duplicate JSON key",
        ):
            resolve_base(self.base, raw_manifest)

    def test_manifest_source_must_return_raw_bytes(self) -> None:
        with self.assertRaisesRegex(
            BaseResolutionError,
            "must return bytes",
        ):
            resolve_base(self.base, lambda _requested_ref: "not raw bytes")

    def test_index_requires_schema_version_two_and_a_manifest_list(self) -> None:
        cases = (
            ("schema version one", self.encode_index(schemaVersion=1), "schemaVersion"),
            ("boolean schema version", self.encode_index(schemaVersion=True), "schemaVersion"),
            ("missing manifests", self.encode_index(manifests=None), "manifests"),
            ("object manifests", self.encode_index(manifests={}), "manifests"),
        )
        for name, raw_manifest, message in cases:
            with self.subTest(name=name), self.assertRaisesRegex(
                BaseResolutionError,
                message,
            ):
                resolve_base(self.base, raw_manifest)

    def test_requires_exactly_one_descriptor_for_each_native_architecture(self) -> None:
        index = json.loads(self.raw_manifest)
        duplicate = dict(index["manifests"][0])
        duplicate["digest"] = "sha256:" + "4" * 64
        index["manifests"].append(duplicate)
        with self.assertRaisesRegex(
            BaseResolutionError,
            "duplicate linux/amd64",
        ):
            resolve_base(self.base, json.dumps(index).encode("utf-8"))

        index = json.loads(self.raw_manifest)
        index["manifests"] = [
            descriptor
            for descriptor in index["manifests"]
            if descriptor["platform"]["architecture"] != "arm64"
        ]
        with self.assertRaisesRegex(
            BaseResolutionError,
            "missing linux/arm64",
        ):
            resolve_base(self.base, json.dumps(index).encode("utf-8"))

    def test_every_index_descriptor_must_have_a_valid_oci_shape(self) -> None:
        mutations = (
            ("not an object", lambda index: index["manifests"].__setitem__(0, []), "descriptor"),
            (
                "invalid media type",
                lambda index: index["manifests"][0].__setitem__("mediaType", "text/plain"),
                "mediaType",
            ),
            (
                "zero size",
                lambda index: index["manifests"][0].__setitem__("size", 0),
                "size",
            ),
            (
                "invalid platform",
                lambda index: index["manifests"][0].__setitem__("platform", "linux/amd64"),
                "platform",
            ),
            (
                "invalid extra digest",
                lambda index: index["manifests"][2].__setitem__("digest", "sha256:bad"),
                "digest",
            ),
        )
        for name, mutate, message in mutations:
            with self.subTest(name=name):
                index = json.loads(self.raw_manifest)
                mutate(index)
                with self.assertRaisesRegex(BaseResolutionError, message):
                    resolve_base(self.base, json.dumps(index).encode("utf-8"))

    def test_required_child_digests_are_exact_lowercase_sha256_values(self) -> None:
        for digest in (
            "sha256:abc",
            "sha512:" + "0" * 64,
            "sha256:" + "A" * 64,
            None,
        ):
            with self.subTest(digest=digest):
                index = json.loads(self.raw_manifest)
                index["manifests"][0]["digest"] = digest
                with self.assertRaisesRegex(
                    BaseResolutionError,
                    "linux/amd64 digest",
                ):
                    resolve_base(self.base, json.dumps(index).encode("utf-8"))

    def test_frozen_resolution_can_be_revalidated_without_an_external_lookup(self) -> None:
        frozen = resolve_base(self.base, self.raw_manifest)

        validated = validate_resolved_base(self.base, frozen)

        self.assertEqual(validated, frozen)

    def test_frozen_child_digests_must_match_the_proven_index_descriptors(self) -> None:
        frozen = resolve_base(self.base, self.raw_manifest)
        frozen["platforms"]["amd64"]["digest"] = "sha256:" + "9" * 64

        with self.assertRaisesRegex(
            BaseResolutionError,
            "platforms do not match index descriptors",
        ):
            validate_resolved_base(self.base, frozen)

    def test_frozen_index_digest_must_match_the_exact_proven_bytes(self) -> None:
        frozen = resolve_base(self.base, self.raw_manifest)
        frozen["index_digest"] = "sha256:" + "9" * 64

        with self.assertRaisesRegex(
            BaseResolutionError,
            "index_digest does not match exact index manifest bytes",
        ):
            validate_resolved_base(self.base, frozen)

    def test_frozen_index_manifest_proof_must_use_canonical_base64(self) -> None:
        frozen = resolve_base(self.base, self.raw_manifest + b" ")
        self.assertTrue(frozen["index_manifest_b64"].endswith("IA=="))
        frozen["index_manifest_b64"] = frozen["index_manifest_b64"][:-3] + "B=="

        with self.assertRaisesRegex(
            BaseResolutionError,
            "index_manifest_b64 must be canonical Base64",
        ):
            validate_resolved_base(self.base, frozen)

    def test_frozen_proof_revalidates_the_index_media_type_offline(self) -> None:
        frozen = resolve_base(self.base, self.raw_manifest)
        index = json.loads(self.raw_manifest)
        index["mediaType"] = "application/vnd.oci.image.manifest.v1+json"
        raw_manifest = json.dumps(index, separators=(",", ":")).encode("utf-8")
        frozen["index_digest"] = (
            f"sha256:{hashlib.sha256(raw_manifest).hexdigest()}"
        )
        frozen["index_manifest_b64"] = base64.b64encode(raw_manifest).decode("ascii")

        with self.assertRaisesRegex(
            BaseResolutionError,
            "unsupported index mediaType",
        ):
            validate_resolved_base(self.base, frozen)

    def test_frozen_resolution_has_an_exact_identity_and_schema(self) -> None:
        frozen = resolve_base(self.base, self.raw_manifest)
        mutations = (
            (
                "extra top-level key",
                lambda value: value.__setitem__("source", "untrusted"),
                "keys must be exactly",
            ),
            (
                "different id",
                lambda value: value.__setitem__("id", "rocky-10.3"),
                "id does not match",
            ),
            (
                "different requested ref",
                lambda value: value.__setitem__(
                    "requested_ref", "quay.io/rockylinux/rockylinux:10.3"
                ),
                "requested_ref does not match",
            ),
            (
                "missing index manifest proof",
                lambda value: value.pop("index_manifest_b64"),
                "keys must be exactly",
            ),
            (
                "missing architecture",
                lambda value: value["platforms"].pop("arm64"),
                "platform keys must be exactly",
            ),
            (
                "wrong platform",
                lambda value: value["platforms"]["arm64"].__setitem__(
                    "platform", "linux/amd64"
                ),
                "platform must be linux/arm64",
            ),
            (
                "malformed child digest",
                lambda value: value["platforms"]["amd64"].__setitem__(
                    "digest", "sha256:bad"
                ),
                "amd64 digest",
            ),
        )
        for name, mutate, message in mutations:
            with self.subTest(name=name):
                altered = json.loads(json.dumps(frozen))
                mutate(altered)
                with self.assertRaisesRegex(BaseResolutionError, message):
                    validate_resolved_base(self.base, altered)

if __name__ == "__main__":
    unittest.main()
