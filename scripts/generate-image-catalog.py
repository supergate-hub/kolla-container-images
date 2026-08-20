#!/usr/bin/env python3
"""Build a static image catalog from repository configuration and GHCR state."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:
    from scripts.profile_resolver import (
        find_stream,
        load_matrix,
        load_profile,
        resolve_profile,
        tag_aliases_for_stream,
    )
except ModuleNotFoundError:
    from profile_resolver import find_stream, load_matrix, load_profile, resolve_profile, tag_aliases_for_stream


INDEX_MEDIA_TYPES = {
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
}
ARCHITECTURES = ("amd64", "arm64")


class CatalogError(ValueError):
    """Raised when catalog input is malformed or cannot be trusted."""


class RegistryNotFound(LookupError):
    """Raised when an expected GHCR package, tag, or manifest is absent."""


@dataclass(frozen=True)
class RegistryManifest:
    raw: bytes
    digest: str


@dataclass(frozen=True)
class Package:
    """A read-only GitHub Packages container package."""

    name: str
    html_url: str | None


class RegistryClient(Protocol):
    def list_tags(self, repository: str) -> set[str]: ...

    def fetch_manifest(self, repository: str, tag: str) -> RegistryManifest: ...


class PackageClient(Protocol):
    def list_container_packages(self, owner: str) -> dict[str, Package]: ...


class GhcrRegistryClient:
    """Small anonymous OCI client for read-only GHCR catalog lookups."""

    def __init__(self, *, opener=urlopen, timeout: int = 30) -> None:
        self._opener = opener
        self._timeout = timeout
        self._tokens: dict[str, str] = {}

    def _request(self, request: Request, *, subject: str):
        try:
            return self._opener(request, timeout=self._timeout)
        except HTTPError as error:
            if error.code == 404:
                raise RegistryNotFound(subject) from error
            raise CatalogError(
                f"GHCR request failed for {subject}: HTTP {error.code}"
            ) from error
        except (URLError, TimeoutError, OSError) as error:
            raise CatalogError(f"GHCR request failed for {subject}: {error}") from error

    def _token(self, repository: str) -> str:
        if repository in self._tokens:
            return self._tokens[repository]
        query = urlencode(
            {
                "service": "ghcr.io",
                "scope": f"repository:{repository}:pull",
            }
        )
        request = Request(f"https://ghcr.io/token?{query}")
        with self._request(request, subject=f"token for {repository}") as response:
            try:
                document = json.loads(response.read())
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise CatalogError("GHCR token response must be JSON") from error
        if not isinstance(document, dict):
            raise CatalogError("GHCR token response must be an object")
        token = document.get("token") or document.get("access_token")
        if not isinstance(token, str) or not token:
            raise CatalogError("GHCR token response is missing token")
        self._tokens[repository] = token
        return token

    def _registry_request(self, repository: str, path: str, *, accept: str) -> Request:
        return Request(
            f"https://ghcr.io/v2/{repository}/{path}",
            headers={
                "Authorization": f"Bearer {self._token(repository)}",
                "Accept": accept,
            },
        )

    def list_tags(self, repository: str) -> set[str]:
        request = self._registry_request(
            repository,
            "tags/list",
            accept="application/json",
        )
        with self._request(request, subject=f"tags for {repository}") as response:
            try:
                document = json.loads(response.read())
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise CatalogError("GHCR tags response must be JSON") from error
        if not isinstance(document, dict):
            raise CatalogError("GHCR tags response must be an object")
        tags = document.get("tags", [])
        if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
            raise CatalogError("GHCR tags response must contain a string tag list")
        return set(tags)

    def fetch_manifest(self, repository: str, tag: str) -> RegistryManifest:
        request = self._registry_request(
            repository,
            f"manifests/{tag}",
            accept=", ".join(sorted(INDEX_MEDIA_TYPES)),
        )
        with self._request(
            request,
            subject=f"manifest {repository}:{tag}",
        ) as response:
            raw = response.read()
            digest = response.headers.get("Docker-Content-Digest")
        if not isinstance(digest, str) or not digest:
            raise CatalogError("GHCR manifest response is missing Docker-Content-Digest")
        return RegistryManifest(raw=raw, digest=digest)


class GithubPackagesClient:
    """Read the complete container-package inventory through GitHub's REST API."""

    def __init__(self, token: str, *, opener=urlopen, timeout: int = 30) -> None:
        if not isinstance(token, str) or not token:
            raise ValueError("GitHub Packages token must be a non-empty string")
        self._token = token
        self._opener = opener
        self._timeout = timeout

    def _request(self, url: str, *, subject: str) -> Any:
        request = Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            return self._opener(request, timeout=self._timeout)
        except HTTPError as error:
            raise CatalogError(
                f"GitHub Packages request failed for {subject}: HTTP {error.code}"
            ) from error
        except (URLError, TimeoutError, OSError) as error:
            raise CatalogError(f"GitHub Packages request failed for {subject}: {error}") from error

    def list_container_packages(self, owner: str) -> dict[str, Package]:
        if not isinstance(owner, str) or not owner:
            raise CatalogError("GitHub Packages owner must be a non-empty string")
        packages: dict[str, Package] = {}
        page = 1
        while True:
            query = urlencode({"package_type": "container", "per_page": "100", "page": str(page)})
            url = f"https://api.github.com/orgs/{owner}/packages?{query}"
            with self._request(url, subject=f"container packages for {owner}") as response:
                try:
                    document = json.loads(response.read())
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise CatalogError("GitHub Packages response must be JSON") from error
            if not isinstance(document, list):
                raise CatalogError("GitHub Packages response must be a list")
            for item in document:
                if not isinstance(item, dict):
                    raise CatalogError("GitHub Packages entry must be an object")
                name = item.get("name")
                html_url = item.get("html_url")
                if not isinstance(name, str) or not name:
                    raise CatalogError("GitHub Packages entry is missing a name")
                if html_url is not None and not isinstance(html_url, str):
                    raise CatalogError("GitHub Packages html_url must be a string")
                if name in packages:
                    raise CatalogError(f"GitHub Packages response repeats package: {name}")
                packages[name] = Package(name=name, html_url=html_url)
            if len(document) < 100:
                return packages
            page += 1


def _missing_architectures() -> dict[str, dict[str, str | None]]:
    return {
        architecture: {"status": "missing", "digest": None}
        for architecture in ARCHITECTURES
    }


def _publication_status(architectures: dict[str, dict[str, str | None]]) -> str:
    available = [
        architectures[architecture]["status"] == "published"
        for architecture in ARCHITECTURES
    ]
    if all(available):
        return "published"
    if any(available):
        return "partial"
    return "missing"


def _parse_architectures(manifest: RegistryManifest) -> dict[str, dict[str, str | None]]:
    expected_digest = f"sha256:{hashlib.sha256(manifest.raw).hexdigest()}"
    if manifest.digest != expected_digest:
        raise CatalogError("registry manifest digest does not match exact manifest bytes")
    try:
        document = json.loads(manifest.raw)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CatalogError("registry manifest must be valid JSON") from error
    if not isinstance(document, dict):
        raise CatalogError("registry manifest must be an object")
    if document.get("schemaVersion") != 2:
        raise CatalogError("registry manifest schemaVersion must be 2")
    if document.get("mediaType") not in INDEX_MEDIA_TYPES:
        raise CatalogError("registry manifest must be a multi-architecture index")
    descriptors = document.get("manifests")
    if not isinstance(descriptors, list):
        raise CatalogError("registry manifest manifests must be a list")

    result = _missing_architectures()
    for descriptor in descriptors:
        if not isinstance(descriptor, dict):
            raise CatalogError("registry manifest descriptor must be an object")
        platform = descriptor.get("platform")
        digest = descriptor.get("digest")
        if not isinstance(platform, dict) or not isinstance(digest, str):
            raise CatalogError("registry manifest descriptor must contain platform and digest")
        architecture = platform.get("architecture")
        if platform.get("os") != "linux" or architecture not in ARCHITECTURES:
            continue
        if not digest.startswith("sha256:") or len(digest) != 71:
            raise CatalogError("registry manifest descriptor digest must be sha256")
        if result[architecture]["status"] == "published":
            raise CatalogError(f"registry manifest repeats {architecture} descriptor")
        result[architecture] = {"status": "published", "digest": digest}
    return result


def _image_architectures(
    registry_client: RegistryClient,
    *,
    repository: str,
    tag: str,
    tag_cache: dict[str, set[str]],
    manifest_cache: dict[tuple[str, str], dict[str, dict[str, str | None]]],
) -> dict[str, dict[str, str | None]]:
    if repository not in tag_cache:
        try:
            tag_cache[repository] = registry_client.list_tags(repository)
        except RegistryNotFound:
            tag_cache[repository] = set()
    if tag not in tag_cache[repository]:
        return _missing_architectures()
    key = (repository, tag)
    if key not in manifest_cache:
        try:
            manifest_cache[key] = _parse_architectures(
                registry_client.fetch_manifest(repository, tag)
            )
        except RegistryNotFound:
            manifest_cache[key] = _missing_architectures()
    return manifest_cache[key]


def _profile_images(profile: dict[str, Any]) -> list[dict[str, str]]:
    groups: dict[str, str] = {}
    for group in profile["build_groups"]:
        group_name = group["name"]
        for image in group["images"]:
            if image in groups:
                raise CatalogError(f"profile image appears in multiple build groups: {image}")
            groups[image] = group_name
    result = []
    for image in profile["images"]:
        name = image["name"]
        if name not in groups:
            raise CatalogError(f"profile image is missing a build group: {name}")
        result.append({"name": name, "service_area": groups[name]})
    return result


def build_catalog(
    matrix: dict[str, Any],
    *,
    registry_client: RegistryClient,
    package_client: PackageClient | None = None,
    stream_ids: list[str] | None = None,
    profile_names: list[str] | None = None,
) -> dict[str, Any]:
    """Return catalog JSON sourced from configured streams and live registry state."""
    for field in ("registry", "owner", "repository", "profiles", "streams"):
        if field not in matrix:
            raise CatalogError(f"matrix is missing {field}")
    requested_streams = set(stream_ids) if stream_ids is not None else None
    configured_profiles = matrix["profiles"]
    requested_profiles = profile_names if profile_names is not None else configured_profiles
    if not all(name in configured_profiles for name in requested_profiles):
        raise CatalogError("requested profile is not configured by the matrix")

    profiles = {name: load_profile(name) for name in requested_profiles}
    tag_cache: dict[str, set[str]] = {}
    manifest_cache: dict[tuple[str, str], dict[str, dict[str, str | None]]] = {}
    releases: dict[str, dict[str, Any]] = {}
    expected_packages: set[str] = set()

    for raw_stream in matrix["streams"]:
        stream_id = raw_stream.get("id") if isinstance(raw_stream, dict) else None
        if requested_streams is not None and stream_id not in requested_streams:
            continue
        if not isinstance(stream_id, str):
            raise CatalogError("matrix stream id must be a string")
        stream = find_stream(matrix, stream_id)
        release = releases.setdefault(
            stream["release"],
            {
                "version": stream["release"],
                "series": stream["release_series"],
                "toolchains": {},
            },
        )
        toolchain = release["toolchains"].setdefault(
            stream["toolchain_version"],
            {
                "version": stream["toolchain_version"],
                "kolla_commit": stream["kolla_commit"],
                "kolla_ansible_commit": stream["kolla_ansible_commit"],
                "targets": [],
            },
        )
        tag = stream["id"]
        target: dict[str, Any] = {
            "stream_id": stream["id"],
            "exact_tag": tag,
            "aliases": tag_aliases_for_stream(matrix, stream),
            "base": {
                "id": stream["base_id"],
                "distro": stream["distro"],
                "os_version": stream["os_version"],
            },
            "profiles": [],
        }
        for name, profile in profiles.items():
            resolved = resolve_profile(profile, stream)
            images = []
            for image in _profile_images(resolved):
                repository = (
                    f"{matrix['owner']}/{matrix['repository']}/{image['name']}"
                )
                package_name = f"{matrix['repository']}/{image['name']}"
                expected_packages.add(package_name)
                architectures = _image_architectures(
                    registry_client,
                    repository=repository,
                    tag=tag,
                    tag_cache=tag_cache,
                    manifest_cache=manifest_cache,
                )
                images.append(
                    {
                        **image,
                        "status": _publication_status(architectures),
                        "package": {"name": package_name, "html_url": None},
                        "architectures": architectures,
                    }
                )
            target["profiles"].append(
                {"name": name, "image_count": len(images), "images": images}
            )
        toolchain["targets"].append(target)

    if requested_streams is not None:
        discovered = {
            target["stream_id"]
            for release in releases.values()
            for toolchain in release["toolchains"].values()
            for target in toolchain["targets"]
        }
        missing = sorted(requested_streams - discovered)
        if missing:
            raise CatalogError(f"requested streams are not configured: {', '.join(missing)}")

    if package_client is None:
        package_inventory = {"status": "registry-only", "unmanaged": []}
        packages: dict[str, Package] = {}
    else:
        packages = package_client.list_container_packages(matrix["owner"])
        package_inventory = {
            "status": "complete",
            "unmanaged": [
                {"name": package.name, "html_url": package.html_url, "status": "unmanaged"}
                for name, package in sorted(packages.items())
                if name not in expected_packages
            ],
        }

    for release in releases.values():
        for toolchain in release["toolchains"].values():
            for target in toolchain["targets"]:
                for profile in target["profiles"]:
                    for image in profile["images"]:
                        package = packages.get(image["package"]["name"])
                        if package is not None:
                            image["package"]["html_url"] = package.html_url

    return {
        "schema_version": 1,
        "registry": matrix["registry"],
        "owner": matrix["owner"],
        "repository": matrix["repository"],
        "package_inventory": package_inventory,
        "releases": [
            {
                **release,
                "toolchains": list(release["toolchains"].values()),
            }
            for release in releases.values()
        ],
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a static Kolla image catalog from GHCR manifests."
    )
    parser.add_argument(
        "--matrix",
        type=Path,
        default=Path("config/build-matrix.json"),
        help="aggregate build matrix to render (default: config/build-matrix.json)",
    )
    parser.add_argument(
        "--stream",
        action="append",
        dest="streams",
        help="exact stream ID to include; repeat to select multiple streams",
    )
    parser.add_argument(
        "--profile",
        action="append",
        dest="profiles",
        help="profile to include; repeat to select multiple profiles",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="catalog.json path to write",
    )
    parser.add_argument(
        "--packages-token-env",
        default="CATALOG_PACKAGES_TOKEN",
        help=(
            "environment variable holding a read-only GitHub Packages token; "
            "when absent, validate configured images through GHCR only"
        ),
    )
    return parser.parse_args(argv)


def main(
    argv: list[str] | None = None,
    *,
    registry_client: RegistryClient | None = None,
    package_client: PackageClient | None = None,
) -> int:
    args = parse_args(argv)
    if package_client is None:
        token = os.environ.get(args.packages_token_env)
        if token:
            package_client = GithubPackagesClient(token)
    catalog = build_catalog(
        load_matrix(args.matrix),
        stream_ids=args.streams,
        profile_names=args.profiles,
        registry_client=registry_client or GhcrRegistryClient(),
        package_client=package_client,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(catalog, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    args.output.with_name("catalog-data.js").write_text(
        "window.IMAGE_CATALOG = " + json.dumps(catalog, ensure_ascii=False) + ";\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
