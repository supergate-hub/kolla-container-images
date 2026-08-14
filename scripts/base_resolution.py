from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import subprocess
from collections.abc import Callable, Mapping
from typing import Any


__all__ = ["BaseResolutionError", "resolve_base", "validate_resolved_base"]


class BaseResolutionError(ValueError):
    """Raised when an OS base image cannot be frozen exactly."""


CONFIG_DIGEST_FIELDS = {"digest", "index_digest", "platform_digests"}
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
INDEX_MEDIA_TYPES = {
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
}
IMAGE_MANIFEST_MEDIA_TYPES = {
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
}
RESOLVED_BASE_KEYS = {
    "id",
    "requested_ref",
    "index_digest",
    "index_manifest_b64",
    "platforms",
}
RESOLVED_PLATFORM_KEYS = {"platform", "digest"}
REQUIRED_ARCHITECTURES = ("amd64", "arm64")


ManifestSource = bytes | Callable[[str], bytes]


def _reject_duplicate_json_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BaseResolutionError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _inspect_raw_manifest(requested_ref: str) -> bytes:
    """Return the exact manifest bytes reported for an image tag."""
    command = [
        "docker",
        "buildx",
        "imagetools",
        "inspect",
        "--raw",
        requested_ref,
    ]
    try:
        result = subprocess.run(
            command,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise BaseResolutionError(
            f"cannot inspect base manifest for {requested_ref}"
        ) from error
    return result.stdout


def _configured_base_identity(base: Mapping[str, Any]) -> tuple[str, str]:
    if not isinstance(base, Mapping):
        raise BaseResolutionError("configured base must be an object")
    forbidden = CONFIG_DIGEST_FIELDS.intersection(base)
    if forbidden:
        raise BaseResolutionError(
            "configured base must not contain digest fields: "
            + ", ".join(sorted(forbidden))
        )
    base_id = base.get("id")
    image = base.get("image")
    tag = base.get("tag")
    if not isinstance(base_id, str) or not base_id:
        raise BaseResolutionError("configured base id must be a non-empty string")
    if not isinstance(image, str) or not image:
        raise BaseResolutionError("configured base image must be a non-empty string")
    if not isinstance(tag, str) or not tag:
        raise BaseResolutionError("configured base tag must be a non-empty string")
    if "@" in image or "@" in tag or DIGEST_RE.fullmatch(tag):
        raise BaseResolutionError(
            "configured base image and tag must not contain a digest"
        )
    return base_id, f"{image}:{tag}"


def _parse_raw_index(
    raw_manifest: bytes,
) -> tuple[str, dict[str, dict[str, str]]]:
    index_digest = f"sha256:{hashlib.sha256(raw_manifest).hexdigest()}"
    try:
        index = json.loads(
            raw_manifest,
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BaseResolutionError(
            "raw manifest must be a JSON object"
        ) from error
    if not isinstance(index, dict):
        raise BaseResolutionError("raw manifest must be a JSON object")
    if type(index.get("schemaVersion")) is not int or index["schemaVersion"] != 2:
        raise BaseResolutionError("index schemaVersion must be integer 2")
    if index.get("mediaType") not in INDEX_MEDIA_TYPES:
        raise BaseResolutionError(
            f"unsupported index mediaType: {index.get('mediaType')!r}"
        )
    if type(index.get("manifests")) is not list:
        raise BaseResolutionError("index manifests must be a list")
    required: dict[str, dict[str, str]] = {}
    for descriptor_index, descriptor in enumerate(index["manifests"]):
        if type(descriptor) is not dict:
            raise BaseResolutionError(
                f"index descriptor[{descriptor_index}] must be an object"
            )
        if descriptor.get("mediaType") not in IMAGE_MANIFEST_MEDIA_TYPES:
            raise BaseResolutionError(
                f"index descriptor[{descriptor_index}] has unsupported mediaType"
            )
        size = descriptor.get("size")
        if type(size) is not int or size <= 0:
            raise BaseResolutionError(
                f"index descriptor[{descriptor_index}] size must be a positive integer"
            )
        platform = descriptor.get("platform")
        if platform is None:
            platform_name = f"index descriptor[{descriptor_index}]"
            architecture = None
            operating_system = None
        else:
            if type(platform) is not dict:
                raise BaseResolutionError(
                    f"index descriptor[{descriptor_index}] platform must be an object"
                )
            architecture = platform.get("architecture")
            operating_system = platform.get("os")
            if not isinstance(architecture, str) or not architecture:
                raise BaseResolutionError(
                    f"index descriptor[{descriptor_index}] platform architecture "
                    "must be a non-empty string"
                )
            if not isinstance(operating_system, str) or not operating_system:
                raise BaseResolutionError(
                    f"index descriptor[{descriptor_index}] platform os "
                    "must be a non-empty string"
                )
            platform_name = f"{operating_system}/{architecture}"
        digest = descriptor.get("digest")
        if not isinstance(digest, str) or not DIGEST_RE.fullmatch(digest):
            raise BaseResolutionError(
                f"{platform_name} digest must be "
                "sha256:<64 lowercase hex chars>"
            )
        if operating_system == "linux" and architecture in REQUIRED_ARCHITECTURES:
            if architecture in required:
                raise BaseResolutionError(
                    f"index contains duplicate linux/{architecture} descriptor"
                )
            required[architecture] = {
                "platform": f"linux/{architecture}",
                "digest": digest,
            }
    for architecture in REQUIRED_ARCHITECTURES:
        if architecture not in required:
            raise BaseResolutionError(
                f"index is missing linux/{architecture} descriptor"
            )
    return index_digest, required


def validate_resolved_base(
    base: Mapping[str, Any], resolved: Any
) -> dict[str, Any]:
    """Validate frozen base provenance without resolving the mutable tag again."""
    base_id, requested_ref = _configured_base_identity(base)
    if type(resolved) is not dict or set(resolved) != RESOLVED_BASE_KEYS:
        raise BaseResolutionError(
            f"resolved base keys must be exactly {sorted(RESOLVED_BASE_KEYS)!r}"
        )
    if resolved.get("id") != base_id:
        raise BaseResolutionError("resolved base id does not match configured base")
    if resolved.get("requested_ref") != requested_ref:
        raise BaseResolutionError(
            "resolved base requested_ref does not match configured base"
        )
    index_digest = resolved.get("index_digest")
    if not isinstance(index_digest, str) or not DIGEST_RE.fullmatch(index_digest):
        raise BaseResolutionError(
            "resolved base index_digest must be sha256:<64 lowercase hex chars>"
        )
    index_manifest_b64 = resolved.get("index_manifest_b64")
    if not isinstance(index_manifest_b64, str):
        raise BaseResolutionError(
            "resolved base index_manifest_b64 must be a Base64 string"
        )
    try:
        raw_manifest = base64.b64decode(index_manifest_b64, validate=True)
    except (ValueError, binascii.Error) as error:
        raise BaseResolutionError(
            "resolved base index_manifest_b64 must be valid Base64"
        ) from error
    if base64.b64encode(raw_manifest).decode("ascii") != index_manifest_b64:
        raise BaseResolutionError(
            "resolved base index_manifest_b64 must be canonical Base64"
        )
    proven_index_digest, proven_platforms = _parse_raw_index(raw_manifest)
    if index_digest != proven_index_digest:
        raise BaseResolutionError(
            "resolved base index_digest does not match exact index manifest bytes"
        )
    platforms = resolved.get("platforms")
    if type(platforms) is not dict or set(platforms) != set(REQUIRED_ARCHITECTURES):
        raise BaseResolutionError(
            "resolved base platform keys must be exactly ['amd64', 'arm64']"
        )
    validated_platforms: dict[str, dict[str, str]] = {}
    for architecture in REQUIRED_ARCHITECTURES:
        record = platforms[architecture]
        if type(record) is not dict or set(record) != RESOLVED_PLATFORM_KEYS:
            raise BaseResolutionError(
                f"resolved base {architecture} keys must be exactly "
                f"{sorted(RESOLVED_PLATFORM_KEYS)!r}"
            )
        expected_platform = f"linux/{architecture}"
        if record.get("platform") != expected_platform:
            raise BaseResolutionError(
                f"resolved base {architecture} platform must be {expected_platform}"
            )
        digest = record.get("digest")
        if not isinstance(digest, str) or not DIGEST_RE.fullmatch(digest):
            raise BaseResolutionError(
                f"resolved base {architecture} digest must be "
                "sha256:<64 lowercase hex chars>"
            )
        validated_platforms[architecture] = {
            "platform": expected_platform,
            "digest": digest,
        }
    if validated_platforms != proven_platforms:
        raise BaseResolutionError(
            "resolved base platforms do not match index descriptors"
        )
    return {
        "id": base_id,
        "requested_ref": requested_ref,
        "index_digest": index_digest,
        "index_manifest_b64": index_manifest_b64,
        "platforms": validated_platforms,
    }


def resolve_base(
    base: Mapping[str, Any],
    manifest_source: ManifestSource | None = None,
    *,
    expected_digest: str | None = None,
) -> dict[str, Any]:
    """Resolve one configured base to exact native image manifest digests."""
    base_id, requested_ref = _configured_base_identity(base)
    if expected_digest is not None and (
        not isinstance(expected_digest, str)
        or not DIGEST_RE.fullmatch(expected_digest)
    ):
        raise BaseResolutionError(
            "expected digest must be sha256:<64 lowercase hex chars>"
        )
    if manifest_source is None:
        manifest_source = _inspect_raw_manifest
    raw_manifest = (
        manifest_source(requested_ref)
        if callable(manifest_source)
        else manifest_source
    )
    if type(raw_manifest) is not bytes:
        raise BaseResolutionError("manifest source must return bytes")
    index_digest, required = _parse_raw_index(raw_manifest)
    if expected_digest is not None:
        if expected_digest != index_digest:
            raise BaseResolutionError(
                "raw manifest digest mismatch: "
                f"expected {expected_digest}, got {index_digest}"
            )
    return validate_resolved_base(
        base,
        {
            "id": base_id,
            "requested_ref": requested_ref,
            "index_digest": index_digest,
            "index_manifest_b64": base64.b64encode(raw_manifest).decode("ascii"),
            "platforms": required,
        },
    )
