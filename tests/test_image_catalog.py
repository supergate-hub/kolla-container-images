from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from scripts.profile_resolver import load_matrix


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "generate-image-catalog.py"
STREAM_ID = "2025.1-rocky-10.2-20.5.0"
TAG = STREAM_ID
DEFAULT_ALIAS = "2025.1-rocky-10.2"


def load_generator():
    spec = importlib.util.spec_from_file_location("image_catalog", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load image catalog generator")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ImageCatalogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.generator = load_generator()

    def index(self) -> bytes:
        return json.dumps(
            {
                "schemaVersion": 2,
                "mediaType": "application/vnd.oci.image.index.v1+json",
                "manifests": [
                    {
                        "mediaType": "application/vnd.oci.image.manifest.v1+json",
                        "digest": "sha256:" + "a" * 64,
                        "size": 111,
                        "platform": {"os": "linux", "architecture": "amd64"},
                    },
                    {
                        "mediaType": "application/vnd.oci.image.manifest.v1+json",
                        "digest": "sha256:" + "b" * 64,
                        "size": 222,
                        "platform": {"os": "linux", "architecture": "arm64"},
                    },
                ],
            },
            separators=(",", ":"),
        ).encode("utf-8")

    def test_builds_profile_rows_from_config_and_architecture_status_from_registry(
        self,
    ) -> None:
        raw_index = self.index()
        generator = self.generator

        class Registry:
            def list_tags(self, repository: str) -> set[str]:
                if repository.endswith("/keystone"):
                    return {TAG, DEFAULT_ALIAS}
                return set()

            def fetch_manifest(self, repository: str, tag: str):
                if repository != "supergate-hub/kolla-container-images/keystone":
                    raise AssertionError(f"unexpected repository: {repository}")
                if tag not in {TAG, DEFAULT_ALIAS}:
                    raise AssertionError(f"unexpected tag: {tag}")
                return generator.RegistryManifest(
                    raw=raw_index,
                    digest="sha256:" + hashlib.sha256(raw_index).hexdigest(),
                )

        catalog = self.generator.build_catalog(
            load_matrix(),
            stream_ids=[STREAM_ID],
            profile_names=["core"],
            registry_client=Registry(),
        )

        self.assertEqual(catalog["schema_version"], 2)
        self.assertEqual(catalog["registry"], "ghcr.io")
        self.assertEqual(catalog["owner"], "supergate-hub")
        self.assertEqual(catalog["repository"], "kolla-container-images")

        release = catalog["releases"][0]
        self.assertEqual(release["version"], "2025.1")
        self.assertEqual(release["series"], "epoxy")
        toolchain = release["toolchains"][0]
        self.assertEqual(toolchain["version"], "20.5.0")
        target = toolchain["targets"][0]
        self.assertEqual(target["stream_id"], STREAM_ID)
        self.assertEqual(target["exact_tag"], TAG)
        self.assertEqual(target["aliases"], ["2025.1-rocky-10.2"])
        self.assertEqual(target["base"]["id"], "rocky-10.2")

        profile = target["profiles"][0]
        self.assertEqual(profile["name"], "core")
        self.assertEqual(profile["image_count"], len(profile["images"]))
        keystone = next(image for image in profile["images"] if image["name"] == "keystone")
        self.assertEqual(keystone["service_area"], "identity")
        self.assertEqual(
            keystone["architectures"],
            {
                "amd64": {"status": "published", "digest": "sha256:" + "a" * 64},
                "arm64": {"status": "published", "digest": "sha256:" + "b" * 64},
            },
        )
        missing = next(image for image in profile["images"] if image["name"] == "glance-api")
        self.assertEqual(
            missing["architectures"],
            {
                "amd64": {"status": "missing", "digest": None},
                "arm64": {"status": "missing", "digest": None},
            },
        )
        self.assertEqual(keystone["status"], "published")
        self.assertEqual(missing["status"], "missing")

    def test_marks_an_image_partial_when_only_one_native_manifest_exists(self) -> None:
        generator = self.generator
        raw_index = json.dumps(
            {
                "schemaVersion": 2,
                "mediaType": "application/vnd.oci.image.index.v1+json",
                "manifests": [
                    {
                        "mediaType": "application/vnd.oci.image.manifest.v1+json",
                        "digest": "sha256:" + "a" * 64,
                        "size": 111,
                        "platform": {"os": "linux", "architecture": "amd64"},
                    }
                ],
            },
            separators=(",", ":"),
        ).encode("utf-8")

        class Registry:
            def list_tags(self, repository: str) -> set[str]:
                return {TAG, DEFAULT_ALIAS} if repository.endswith("/keystone") else set()

            def fetch_manifest(self, repository: str, tag: str):
                return generator.RegistryManifest(
                    raw=raw_index,
                    digest="sha256:" + hashlib.sha256(raw_index).hexdigest(),
                )

        catalog = self.generator.build_catalog(
            load_matrix(),
            stream_ids=[STREAM_ID],
            profile_names=["core"],
            registry_client=Registry(),
        )
        images = catalog["releases"][0]["toolchains"][0]["targets"][0]["profiles"][0]["images"]
        keystone = next(image for image in images if image["name"] == "keystone")
        self.assertEqual(keystone["status"], "partial")
        self.assertEqual(keystone["architectures"]["arm64"]["status"], "missing")

    def test_packages_inventory_paginates_for_a_separate_diagnostic_consumer(self) -> None:
        generator = self.generator
        requests: list[str] = []

        class Response:
            def __init__(self, body: bytes):
                self.body = body

            def read(self) -> bytes:
                return self.body

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback) -> None:
                return None

        first_page = [
            {"name": f"kolla-container-images/unused-{number}", "html_url": None}
            for number in range(99)
        ] + [{"name": "kolla-container-images/keystone", "html_url": "https://example.test/keystone"}]
        second_page = [{"name": "kolla-container-images/unmanaged", "html_url": "https://example.test/unmanaged"}]

        def opener(request, *, timeout: int):
            requests.append(request.full_url)
            page = parse_qs(urlparse(request.full_url).query)["page"][0]
            return Response(json.dumps(first_page if page == "1" else second_page).encode("utf-8"))

        client = generator.GithubPackagesClient("read-only-token", opener=opener)
        packages = client.list_container_packages("supergate-hub")
        self.assertEqual(len(packages), 101)
        self.assertEqual(len(requests), 2)

    def test_full_catalog_never_enumerates_every_organization_package(self) -> None:
        generator = self.generator
        package_lookups: list[str] = []

        class EmptyRegistry:
            def list_tags(self, repository: str) -> set[str]:
                return set()

            def fetch_manifest(self, repository: str, tag: str):
                raise AssertionError("missing tags do not request manifests")

        class ManagedPackagesOnly:
            def list_container_packages(self, owner: str):
                raise AssertionError("catalog rendering must not enumerate organization packages")

            def get_container_package(self, owner: str, name: str):
                package_lookups.append(name)
                return None

        catalog = generator.build_catalog(
            load_matrix(),
            stream_ids=[STREAM_ID],
            profile_names=["core"],
            registry_client=EmptyRegistry(),
            package_client=ManagedPackagesOnly(),
        )

        self.assertEqual(catalog["package_inventory"], {"status": "managed-only", "unmanaged": []})
        self.assertIn("kolla-container-images/keystone", package_lookups)

    def test_incremental_lookup_reads_only_the_new_container_package(self) -> None:
        generator = self.generator
        requested: list[str] = []

        class Response:
            def read(self) -> bytes:
                return json.dumps(
                    {"name": "kolla-container-images/keystone", "html_url": "https://example.test"}
                ).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback) -> None:
                return None

        def opener(request, *, timeout: int):
            requested.append(request.full_url)
            return Response()

        package = generator.GithubPackagesClient("read-only-token", opener=opener).get_container_package(
            "supergate-hub", "kolla-container-images/keystone"
        )

        self.assertEqual(package.html_url, "https://example.test")
        self.assertIn("kolla-container-images%2Fkeystone", requested[0])

    def test_ghcr_client_lists_tags_and_reads_the_exact_manifest(self) -> None:
        raw_index = self.index()
        requested: list[str] = []

        class Response:
            def __init__(self, body: bytes, *, headers: dict[str, str] | None = None):
                self.body = body
                self.headers = headers or {}

            def read(self) -> bytes:
                return self.body

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback) -> None:
                return None

        def opener(request, *, timeout: int):
            requested.append(request.full_url)
            if request.full_url.startswith("https://ghcr.io/token?"):
                return Response(b'{"token":"catalog-token"}')
            if request.full_url.endswith("/tags/list"):
                return Response(json.dumps({"tags": [TAG]}).encode("utf-8"))
            if request.full_url.endswith(f"/manifests/{TAG}"):
                return Response(
                    raw_index,
                    headers={
                        "Docker-Content-Digest": (
                            "sha256:" + hashlib.sha256(raw_index).hexdigest()
                        )
                    },
                )
            raise AssertionError(f"unexpected registry request: {request.full_url}")

        client = self.generator.GhcrRegistryClient(opener=opener)
        repository = "supergate-hub/kolla-container-images/keystone"

        self.assertEqual(client.list_tags(repository), {TAG})
        manifest = client.fetch_manifest(repository, TAG)

        self.assertEqual(manifest.raw, raw_index)
        self.assertEqual(
            manifest.digest,
            "sha256:" + hashlib.sha256(raw_index).hexdigest(),
        )
        self.assertEqual(sum(url.startswith("https://ghcr.io/token?") for url in requested), 1)

    def test_cli_writes_a_static_catalog_json(self) -> None:
        class EmptyRegistry:
            def list_tags(self, repository: str) -> set[str]:
                return set()

            def fetch_manifest(self, repository: str, tag: str):
                raise AssertionError("a missing tag must not request a manifest")

        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "site" / "catalog.json"

            exit_code = self.generator.main(
                [
                    "--stream",
                    STREAM_ID,
                    "--profile",
                    "core",
                    "--output",
                    str(output),
                ],
                registry_client=EmptyRegistry(),
            )

            self.assertEqual(exit_code, 0)
            catalog = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(catalog["releases"][0]["version"], "2025.1")
            self.assertEqual(
                catalog["releases"][0]["toolchains"][0]["targets"][0]["exact_tag"],
                TAG,
            )
            fallback = output.with_name("catalog-data.js")
            self.assertEqual(
                fallback.read_text(encoding="utf-8"),
                "window.IMAGE_CATALOG = " + json.dumps(catalog, ensure_ascii=False) + ";\n",
            )

    def test_incremental_catalog_reuses_unchanged_registry_state_without_queries(self) -> None:
        generator = self.generator
        raw_index = self.index()

        class PublishedRegistry:
            def list_tags(self, repository: str) -> set[str]:
                return {TAG, DEFAULT_ALIAS} if repository.endswith("/keystone") else set()

            def fetch_manifest(self, repository: str, tag: str):
                return generator.RegistryManifest(
                    raw=raw_index,
                    digest="sha256:" + hashlib.sha256(raw_index).hexdigest(),
                )

        baseline = generator.build_catalog(
            load_matrix(),
            stream_ids=[STREAM_ID],
            profile_names=["core"],
            registry_client=PublishedRegistry(),
        )

        class NoRegistryCalls:
            def list_tags(self, repository: str) -> set[str]:
                raise AssertionError(f"unchanged catalog queried tags: {repository}")

            def fetch_manifest(self, repository: str, tag: str):
                raise AssertionError(f"unchanged catalog queried manifest: {repository}:{tag}")

        catalog = generator.build_catalog(
            load_matrix(),
            stream_ids=[STREAM_ID],
            profile_names=["core"],
            registry_client=NoRegistryCalls(),
            baseline=baseline,
            mode="incremental",
        )

        self.assertEqual(catalog, baseline)

    def test_publish_refresh_queries_only_the_images_named_by_the_summary(self) -> None:
        generator = self.generator
        raw_index = self.index()

        class EmptyRegistry:
            def list_tags(self, repository: str) -> set[str]:
                return set()

            def fetch_manifest(self, repository: str, tag: str):
                raise AssertionError("missing tags do not request manifests")

        baseline = generator.build_catalog(
            load_matrix(),
            stream_ids=[STREAM_ID],
            profile_names=["core"],
            registry_client=EmptyRegistry(),
        )
        queried: list[str] = []

        class KeystoneRegistry:
            def list_tags(self, repository: str) -> set[str]:
                queried.append(repository)
                return {TAG, DEFAULT_ALIAS}

            def fetch_manifest(self, repository: str, tag: str):
                return generator.RegistryManifest(
                    raw=raw_index,
                    digest="sha256:" + hashlib.sha256(raw_index).hexdigest(),
                )

        catalog = generator.build_catalog(
            load_matrix(),
            stream_ids=[STREAM_ID],
            profile_names=["core"],
            registry_client=KeystoneRegistry(),
            baseline=baseline,
            mode="publish",
            refresh_images={(STREAM_ID, "keystone")},
        )

        self.assertEqual(queried, ["supergate-hub/kolla-container-images/keystone"])
        images = catalog["releases"][0]["toolchains"][0]["targets"][0]["profiles"][0]["images"]
        self.assertEqual(next(image for image in images if image["name"] == "keystone")["status"], "published")
        self.assertEqual(next(image for image in images if image["name"] == "glance-api")["status"], "missing")

    def test_publish_summary_contract_binds_images_to_the_main_toolchain(self) -> None:
        generator = self.generator
        matrix = load_matrix()
        stream = generator.find_stream(matrix, STREAM_ID)
        summary = {
            "stream": STREAM_ID,
            "profile": "core",
            "kolla": {"version": stream["kolla_version"], "commit": stream["kolla_commit"]},
            "kolla_ansible": {
                "version": stream["kolla_ansible_version"],
                "commit": stream["kolla_ansible_commit"],
            },
            "images": [
                {"image": "keystone", "manifest_digest": "sha256:" + "c" * 64}
            ],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "publish-summary.json"
            path.write_text(json.dumps(summary), encoding="utf-8")
            selected, digests = generator.publish_refresh_contract(path, matrix)

        self.assertEqual(selected, {(STREAM_ID, "keystone")})
        self.assertEqual(digests[(STREAM_ID, "keystone")], "sha256:" + "c" * 64)

    def test_publish_refresh_retries_a_manifest_that_is_not_visible_yet(self) -> None:
        generator = self.generator
        raw_index = self.index()

        class EmptyRegistry:
            def list_tags(self, repository: str) -> set[str]:
                return set()

            def fetch_manifest(self, repository: str, tag: str):
                raise AssertionError("missing tags do not request manifests")

        baseline = generator.build_catalog(
            load_matrix(),
            stream_ids=[STREAM_ID],
            profile_names=["core"],
            registry_client=EmptyRegistry(),
        )
        attempts = 0
        delays: list[float] = []

        class DelayedRegistry:
            def list_tags(self, repository: str) -> set[str]:
                nonlocal attempts
                attempts += 1
                return set() if attempts == 1 else {TAG, DEFAULT_ALIAS}

            def fetch_manifest(self, repository: str, tag: str):
                return generator.RegistryManifest(
                    raw=raw_index,
                    digest="sha256:" + hashlib.sha256(raw_index).hexdigest(),
                )

        catalog = generator.build_catalog(
            load_matrix(),
            stream_ids=[STREAM_ID],
            profile_names=["core"],
            registry_client=DelayedRegistry(),
            baseline=baseline,
            mode="publish",
            refresh_images={(STREAM_ID, "keystone")},
            sleeper=delays.append,
        )

        self.assertEqual(delays, [1.0])
        images = catalog["releases"][0]["toolchains"][0]["targets"][0]["profiles"][0]["images"]
        self.assertEqual(next(image for image in images if image["name"] == "keystone")["status"], "published")


if __name__ == "__main__":
    unittest.main()
