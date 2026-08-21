#!/usr/bin/env python3
"""Build a static image catalog from repository configuration and GHCR state."""

from __future__ import annotations

import argparse
import copy
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Callable, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
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
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


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

    def get_container_package(self, owner: str, name: str) -> Package | None: ...


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

    def get_container_package(self, owner: str, name: str) -> Package | None:
        if not isinstance(owner, str) or not owner or not isinstance(name, str) or not name:
            raise CatalogError("GitHub package owner and name must be non-empty strings")
        url = (
            f"https://api.github.com/orgs/{owner}/packages/container/"
            f"{quote(name, safe='')}"
        )
        request = Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with self._opener(request, timeout=self._timeout) as response:
                document = json.loads(response.read())
        except HTTPError as error:
            if error.code == 404:
                return None
            raise CatalogError(f"GitHub Packages request failed for {name}: HTTP {error.code}") from error
        except (URLError, TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CatalogError(f"GitHub Packages request failed for {name}: {error}") from error
        if not isinstance(document, dict) or document.get("name") != name:
            raise CatalogError("GitHub Packages entry does not match requested package")
        html_url = document.get("html_url")
        if html_url is not None and not isinstance(html_url, str):
            raise CatalogError("GitHub Packages html_url must be a string")
        return Package(name=name, html_url=html_url)


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
    manifest_digest_cache: dict[tuple[str, str], str | None],
    not_found_retries: int = 0,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, dict[str, str | None]]:
    key = (repository, tag)
    if key in manifest_cache:
        return manifest_cache[key]
    for attempt in range(not_found_retries + 1):
        try:
            if repository not in tag_cache or attempt:
                tag_cache[repository] = registry_client.list_tags(repository)
        except RegistryNotFound:
            tag_cache[repository] = set()
        if tag in tag_cache[repository]:
            try:
                manifest = registry_client.fetch_manifest(repository, tag)
                manifest_cache[key] = _parse_architectures(manifest)
                manifest_digest_cache[key] = manifest.digest
                return manifest_cache[key]
            except RegistryNotFound:
                pass
        if attempt < not_found_retries:
            sleeper(float(2**attempt))
    manifest_cache[key] = _missing_architectures()
    manifest_digest_cache[key] = None
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


def _canonical_digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _baseline_targets(catalog: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not isinstance(catalog, dict) or catalog.get("schema_version") != 2:
        return {}
    result: dict[str, dict[str, Any]] = {}
    for release in catalog.get("releases", []):
        for toolchain in release.get("toolchains", []):
            for target in toolchain.get("targets", []):
                stream_id = target.get("stream_id")
                if not isinstance(stream_id, str) or stream_id in result:
                    raise CatalogError("baseline catalog has invalid or duplicate stream_id")
                result[stream_id] = target
    return result


def _baseline_images(catalog: dict[str, Any] | None) -> dict[tuple[str, str, str], dict[str, Any]]:
    result: dict[tuple[str, str, str], dict[str, Any]] = {}
    for stream_id, target in _baseline_targets(catalog).items():
        for profile in target.get("profiles", []):
            profile_name = profile.get("name")
            if not isinstance(profile_name, str):
                raise CatalogError("baseline catalog profile name must be a string")
            for image in profile.get("images", []):
                image_name = image.get("name")
                key = (stream_id, profile_name, image_name)
                if not isinstance(image_name, str) or key in result:
                    raise CatalogError("baseline catalog image identity is invalid or duplicated")
                result[key] = image
    return result


def _same_target_definition(previous: dict[str, Any] | None, current: dict[str, Any]) -> bool:
    if not isinstance(previous, dict):
        return False
    return all(
        previous.get(key) == current[key]
        for key in ("stream_id", "exact_tag", "aliases", "base")
    )


def load_catalog(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CatalogError(f"catalog baseline must be valid JSON: {path}") from error
    if not isinstance(value, dict):
        raise CatalogError("catalog baseline must be an object")
    return value


def publish_refresh_contract(
    summary_path: Path,
    matrix: dict[str, Any],
) -> tuple[set[tuple[str, str]], dict[tuple[str, str], str]]:
    """Return the exact catalog entries which a successful publish may refresh."""
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CatalogError("publish summary must be valid JSON") from error
    if not isinstance(summary, dict):
        raise CatalogError("publish summary must be an object")
    stream_id = summary.get("stream")
    profile_name = summary.get("profile")
    images = summary.get("images")
    if not isinstance(stream_id, str) or not isinstance(profile_name, str):
        raise CatalogError("publish summary must identify stream and profile")
    if not isinstance(images, list) or not images:
        raise CatalogError("publish summary must contain published images")
    stream = find_stream(matrix, stream_id)
    kolla = summary.get("kolla")
    kolla_ansible = summary.get("kolla_ansible")
    if not isinstance(kolla, dict) or not isinstance(kolla_ansible, dict):
        raise CatalogError("publish summary toolchain pins are missing")
    if (
        kolla.get("commit") != stream["kolla_commit"]
        or kolla_ansible.get("commit") != stream["kolla_ansible_commit"]
        or kolla.get("version") != stream["kolla_version"]
        or kolla_ansible.get("version") != stream["kolla_ansible_version"]
    ):
        raise CatalogError("publish summary toolchain does not match main catalog")
    profile = resolve_profile(load_profile(profile_name), stream)
    allowed_images = {image["name"] for image in profile["images"]}
    selected: set[tuple[str, str]] = set()
    expected: dict[tuple[str, str], str] = {}
    for entry in images:
        if not isinstance(entry, dict):
            raise CatalogError("publish summary image entry must be an object")
        name = entry.get("image")
        digest = entry.get("manifest_digest")
        if not isinstance(name, str) or name not in allowed_images:
            raise CatalogError("publish summary image is outside the configured profile")
        if not isinstance(digest, str) or not DIGEST_RE.fullmatch(digest):
            raise CatalogError("publish summary manifest digest is invalid")
        key = (stream_id, name)
        if key in selected:
            raise CatalogError("publish summary repeats an image")
        selected.add(key)
        expected[key] = digest
    return selected, expected


def build_catalog(
    matrix: dict[str, Any],
    *,
    registry_client: RegistryClient,
    package_client: PackageClient | None = None,
    stream_ids: list[str] | None = None,
    profile_names: list[str] | None = None,
    baseline: dict[str, Any] | None = None,
    mode: str = "full",
    refresh_images: set[tuple[str, str]] | None = None,
    expected_manifest_digests: dict[tuple[str, str], str] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Return catalog JSON sourced from configured streams and live registry state."""
    if mode not in {"full", "incremental", "publish"}:
        raise CatalogError(f"unsupported catalog refresh mode: {mode}")
    if mode == "publish" and not refresh_images:
        raise CatalogError("publish refresh requires at least one image")
    for field in ("registry", "owner", "repository", "profiles", "streams"):
        if field not in matrix:
            raise CatalogError(f"matrix is missing {field}")
    requested_streams = set(stream_ids) if stream_ids is not None else None
    configured_profiles = matrix["profiles"]
    requested_profiles = profile_names if profile_names is not None else configured_profiles
    if not all(name in configured_profiles for name in requested_profiles):
        raise CatalogError("requested profile is not configured by the matrix")

    profiles = {name: load_profile(name) for name in requested_profiles}
    previous_targets = _baseline_targets(baseline)
    previous_images = _baseline_images(baseline)
    baseline_is_usable = bool(previous_targets)
    if mode != "full" and baseline is not None and not baseline_is_usable:
        # Schema v1 is intentionally rebuilt once rather than guessed from a stale snapshot.
        mode = "full"
    tag_cache: dict[str, set[str]] = {}
    manifest_cache: dict[tuple[str, str], dict[str, dict[str, str | None]]] = {}
    manifest_digest_cache: dict[tuple[str, str], str | None] = {}
    package_cache: dict[str, Package | None] = {}
    releases: dict[str, dict[str, Any]] = {}

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
        target_is_unchanged = _same_target_definition(
            previous_targets.get(stream["id"]), target
        )
        for name, profile in profiles.items():
            resolved = resolve_profile(profile, stream)
            images = []
            for image in _profile_images(resolved):
                repository = (
                    f"{matrix['owner']}/{matrix['repository']}/{image['name']}"
                )
                package_name = f"{matrix['repository']}/{image['name']}"
                previous = previous_images.get((stream["id"], name, image["name"]))
                must_refresh = (
                    mode == "full"
                    or not target_is_unchanged
                    or previous is None
                    or (stream["id"], image["name"]) in (refresh_images or set())
                )
                if must_refresh:
                    architectures = _image_architectures(
                        registry_client,
                        repository=repository,
                        tag=tag,
                        tag_cache=tag_cache,
                        manifest_cache=manifest_cache,
                        manifest_digest_cache=manifest_digest_cache,
                        not_found_retries=5 if mode == "publish" else 0,
                        sleeper=sleeper,
                    )
                    manifest_digest = manifest_digest_cache[(repository, tag)]
                    expected_digest = (expected_manifest_digests or {}).get(
                        (stream["id"], image["name"])
                    )
                    if expected_digest is not None and manifest_digest != expected_digest:
                        raise CatalogError(
                            "published manifest digest does not match publish summary: "
                            f"{image['name']}:{tag}"
                        )
                    for alias in target["aliases"]:
                        alias_architectures = _image_architectures(
                            registry_client,
                            repository=repository,
                            tag=alias,
                            tag_cache=tag_cache,
                            manifest_cache=manifest_cache,
                            manifest_digest_cache=manifest_digest_cache,
                            not_found_retries=5 if mode == "publish" else 0,
                            sleeper=sleeper,
                        )
                        alias_digest = manifest_digest_cache[(repository, alias)]
                        if alias_architectures != architectures or alias_digest != manifest_digest:
                            raise CatalogError(
                                "default alias does not match exact image manifest: "
                                f"{image['name']}:{alias}"
                            )
                    package_html_url = None
                    if package_client is not None:
                        if package_name not in package_cache:
                            package_cache[package_name] = package_client.get_container_package(
                                matrix["owner"], package_name
                            )
                        package = package_cache[package_name]
                        package_html_url = package.html_url if package else None
                    publication = {
                        "status": _publication_status(architectures),
                        "manifest_digest": manifest_digest,
                        "package": {"name": package_name, "html_url": package_html_url},
                        "architectures": architectures,
                    }
                else:
                    publication = {
                        "status": previous.get("status"),
                        "manifest_digest": previous.get("manifest_digest"),
                        "package": copy.deepcopy(previous.get("package")),
                        "architectures": copy.deepcopy(previous.get("architectures")),
                    }
                    if (
                        publication["status"] not in {"published", "partial", "missing"}
                        or not isinstance(publication["package"], dict)
                        or not isinstance(publication["architectures"], dict)
                    ):
                        raise CatalogError("baseline catalog image publication state is invalid")
                images.append(
                    {
                        **image,
                        **publication,
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

    package_inventory = {
        "status": "managed-only" if package_client is not None else "registry-only",
        "unmanaged": [],
    }

    return {
        "schema_version": 2,
        "configuration_digest": _canonical_digest({"matrix": matrix, "profiles": profiles}),
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
        "--mode",
        choices=("full", "incremental", "publish"),
        default="full",
        help="full reconciliation, configuration delta, or successful publish refresh",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        help="existing gh-pages catalog.json required by incremental and publish modes",
    )
    parser.add_argument(
        "--publish-summary",
        type=Path,
        help="validated terminal publish summary required by publish mode",
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
    if args.mode in {"incremental", "publish"} and args.baseline is None:
        raise CatalogError(f"{args.mode} mode requires --baseline")
    if args.mode == "publish" and args.publish_summary is None:
        raise CatalogError("publish mode requires --publish-summary")
    if args.mode != "publish" and args.publish_summary is not None:
        raise CatalogError("--publish-summary is only valid in publish mode")
    matrix = load_matrix(args.matrix)
    baseline = load_catalog(args.baseline) if args.baseline is not None else None
    refresh_images: set[tuple[str, str]] | None = None
    expected_manifest_digests: dict[tuple[str, str], str] | None = None
    if args.publish_summary is not None:
        refresh_images, expected_manifest_digests = publish_refresh_contract(
            args.publish_summary,
            matrix,
        )
    if package_client is None:
        token = os.environ.get(args.packages_token_env)
        if token:
            package_client = GithubPackagesClient(token)
    catalog = build_catalog(
        matrix,
        stream_ids=args.streams,
        profile_names=args.profiles,
        registry_client=registry_client or GhcrRegistryClient(),
        package_client=package_client,
        baseline=baseline,
        mode=args.mode,
        refresh_images=refresh_images,
        expected_manifest_digests=expected_manifest_digests,
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
