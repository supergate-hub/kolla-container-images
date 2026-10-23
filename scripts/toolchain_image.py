"""Pins, frozen plan, image checks and handoff lock for the Kolla-Ansible toolchain image.

The toolchain image packages one exact Kolla-Ansible release with a hashed
Python environment and pinned Ansible collections, so a consumer runs that
release as a container instead of assembling a virtual environment by hand.
This module validates the reviewed pins under ``toolchain/<version>/``,
renders the frozen build plan, checks a built image and writes the generic
toolchain lock. Nothing here connects to OpenStack hosts; running the tool
against an environment belongs to the consumer, like every other handoff.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

try:
    from scripts.base_resolution import resolve_base
    from scripts.registry_manifest import inspect_raw_manifest
except ModuleNotFoundError:
    from base_resolution import resolve_base
    from registry_manifest import inspect_raw_manifest


ROOT = Path(__file__).resolve().parents[1]
MATRIX_PATH = ROOT / "config" / "build-matrix.json"
SOURCE_SET_ROOT = ROOT / "config" / "openstack-sources"
TOOLCHAIN_ROOT = ROOT / "toolchain"
IMAGE_NAME = "kolla-ansible"
ARCH_RUNNERS = {"amd64": "ubuntu-24.04", "arm64": "ubuntu-24.04-arm"}
VENV = "/opt/kolla-ansible/venv"
COLLECTIONS_PATH = "/usr/share/ansible/collections"
GALAXY_ARTIFACTS = (
    "https://galaxy.ansible.com/api/v3/plugin/ansible/content/published/collections/artifacts/"
)
GALAXY_INDEX = "https://galaxy.ansible.com/api/v3/plugin/ansible/content/published/collections/index"
KOLLA_COLLECTION_REPOSITORY = "https://opendev.org/openstack/ansible-collection-kolla"

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
COLLECTION_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
CANDIDATE_RE = re.compile(r"^[1-9][0-9]*-[1-9][0-9]*$")
REQUIREMENT_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([A-Za-z0-9.+!-]+)$")
CONSTRAINT_RE = re.compile(r"^(<=|>=|==|!=|<|>)([0-9]+(?:\.[0-9]+)*)$")

TOOLCHAIN_KEYS = {
    "schema_version",
    "toolchain",
    "release",
    "base",
    "kolla_ansible",
    "python_requirements",
    "collections",
    "kolla_collection",
}
PYTHON_KEYS = {"inputs", "upper_constraints_sha256", "lock"}
COLLECTION_KEYS = {"name", "constraint", "version", "url", "sha256"}
KOLLA_COLLECTION_KEYS = {"name", "release", "repository", "commit", "galaxy_version"}
EXPECTED_MANIFEST_MEDIA_TYPES = {
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
}


class ToolchainError(ValueError):
    """Raised when toolchain pins, plans or image evidence are not exact."""


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ToolchainError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicates)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def normalize_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def satisfies(version: str, constraint: str) -> bool:
    """Return whether an x.y.z version meets a comma-separated constraint."""
    if not VERSION_RE.fullmatch(version):
        raise ToolchainError(f"collection version must be x.y.z: {version}")
    actual = _version_key(version)
    for clause in constraint.split(","):
        match = CONSTRAINT_RE.fullmatch(clause.strip())
        if not match:
            raise ToolchainError(f"unsupported collection constraint: {constraint}")
        operator, bound_text = match.groups()
        bound = _version_key(bound_text)
        width = max(len(actual), len(bound))
        left = actual + (0,) * (width - len(actual))
        right = bound + (0,) * (width - len(bound))
        ok = {
            "<": left < right,
            "<=": left <= right,
            ">": left > right,
            ">=": left >= right,
            "==": left == right,
            "!=": left != right,
        }[operator]
        if not ok:
            return False
    return True


def parse_requirements_lock(text: str) -> dict[str, dict[str, Any]]:
    """Parse a hashed pip lock into normalized name -> {name, version, hashes}."""
    logical: list[str] = []
    current = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#"):
            line = ""
        if not line:
            if current:
                raise ToolchainError("requirements lock has a dangling continuation")
            continue
        if line.endswith("\\"):
            current += line[:-1].strip() + " "
            continue
        logical.append(current + line)
        current = ""
    if current:
        raise ToolchainError("requirements lock ends with a continuation")
    entries: dict[str, dict[str, Any]] = {}
    for line in logical:
        tokens = line.split()
        match = REQUIREMENT_RE.fullmatch(tokens[0])
        if not match:
            raise ToolchainError(f"requirements lock entry must be name==version: {tokens[0]}")
        hashes = []
        for token in tokens[1:]:
            if not token.startswith("--hash=sha256:") or not SHA256_RE.fullmatch(token[14:]):
                raise ToolchainError(f"requirements lock entry has an invalid option: {token}")
            hashes.append(token[14:])
        if not hashes:
            raise ToolchainError(f"requirements lock entry has no hash: {tokens[0]}")
        name, version = match.groups()
        key = normalize_name(name)
        if key in entries:
            raise ToolchainError(f"requirements lock pins {name} twice")
        entries[key] = {"name": name, "version": version, "hashes": hashes}
    if not entries:
        raise ToolchainError("requirements lock is empty")
    return entries


def _exact_keys(value: Any, keys: set[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ToolchainError(f"{label} must have exactly: {', '.join(sorted(keys))}")
    return value


def toolchain_versions(root: Path = ROOT) -> list[str]:
    directory = root / "toolchain"
    return sorted(
        (path.name for path in directory.iterdir() if (path / "toolchain.json").is_file()),
        key=_version_key,
    )


def _release_upper_constraints_sha256(matrix: Mapping[str, Any], release: str, root: Path) -> str:
    source_set_id = matrix["releases"][release]["source_set"]
    source_set = load_json(root / "config" / "openstack-sources" / f"{source_set_id}.json")
    project = source_set["projects"].get("openstack/requirements", {})
    digest = project.get("upper_constraints_sha256")
    if isinstance(digest, str) and SHA256_RE.fullmatch(digest):
        return digest
    raise ToolchainError(f"source-set {source_set_id} records no upper-constraints digest")


def load_toolchain(version: str, *, root: Path = ROOT) -> dict[str, Any]:
    """Load and fully validate one reviewed toolchain pin set."""
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        raise ToolchainError(f"toolchain version must be x.y.z: {version!r}")
    directory = root / "toolchain" / version
    manifest = _exact_keys(load_json(directory / "toolchain.json"), TOOLCHAIN_KEYS, "toolchain.json")
    matrix = load_json(root / "config" / "build-matrix.json")
    if manifest["schema_version"] != 1:
        raise ToolchainError("toolchain.json schema_version must be 1")
    if manifest["toolchain"] != version:
        raise ToolchainError("toolchain.json version does not match its directory")
    matrix_toolchain = matrix["toolchains"].get(version)
    if matrix_toolchain is None:
        raise ToolchainError(f"toolchain {version} is not in config/build-matrix.json")
    release = manifest["release"]
    if release not in matrix["releases"]:
        raise ToolchainError(f"release {release!r} is not in config/build-matrix.json")
    if manifest["base"] not in matrix["bases"]:
        raise ToolchainError(f"base {manifest['base']!r} is not in config/build-matrix.json")
    if matrix["bases"][manifest["base"]]["distro"] != "ubuntu":
        raise ToolchainError("toolchain/Dockerfile installs with apt, so the base must be Ubuntu")
    if not any(
        stream["release"] == release and stream["toolchain"] == version and stream["publish_enabled"]
        for stream in matrix["streams"]
    ):
        raise ToolchainError(f"no enabled stream joins release {release} and toolchain {version}")

    kolla_ansible = _exact_keys(manifest["kolla_ansible"], {"version", "commit"}, "kolla_ansible")
    if kolla_ansible["version"] != version:
        raise ToolchainError("kolla_ansible.version must equal the toolchain version")
    if kolla_ansible["commit"] != matrix_toolchain["kolla_ansible"]["commit"]:
        raise ToolchainError("kolla_ansible.commit differs from config/build-matrix.json")

    python = _exact_keys(manifest["python_requirements"], PYTHON_KEYS, "python_requirements")
    if python["upper_constraints_sha256"] != _release_upper_constraints_sha256(matrix, release, root):
        raise ToolchainError("upper-constraints digest differs from the release source-set")
    if python["lock"] != "requirements.lock":
        raise ToolchainError("python_requirements.lock must be requirements.lock")
    inputs = python["inputs"]
    if not isinstance(inputs, list) or not all(isinstance(item, str) and item for item in inputs):
        raise ToolchainError("python_requirements.inputs must be a list of requirement strings")
    if f"kolla-ansible=={version}" not in inputs:
        raise ToolchainError("python_requirements.inputs must pin the toolchain's kolla-ansible")
    lock_path = directory / "requirements.lock"
    entries = parse_requirements_lock(lock_path.read_text(encoding="utf-8"))
    pinned = entries.get("kolla-ansible")
    if pinned is None or pinned["version"] != version:
        raise ToolchainError("requirements.lock must pin kolla-ansible to the toolchain version")
    if "pip" in entries:
        raise ToolchainError("requirements.lock must not install pip into the image")

    collections = manifest["collections"]
    if not isinstance(collections, list) or not collections:
        raise ToolchainError("collections must be a non-empty list")
    for collection in collections:
        keys = set(collection) if isinstance(collection, dict) else set()
        if keys not in (COLLECTION_KEYS, COLLECTION_KEYS | {"required_by"}):
            raise ToolchainError("collection entries must have name, constraint, version, url, sha256")
    pinned_names = {collection["name"] for collection in collections}
    names = []
    for collection in collections:
        name = collection["name"]
        if not isinstance(name, str) or not COLLECTION_RE.fullmatch(name):
            raise ToolchainError(f"invalid collection name: {name!r}")
        if not satisfies(collection["version"], collection["constraint"]):
            raise ToolchainError(f"{name} {collection['version']} violates {collection['constraint']}")
        namespace, short = name.split(".")
        expected_url = f"{GALAXY_ARTIFACTS}{namespace}-{short}-{collection['version']}.tar.gz"
        if collection["url"] != expected_url:
            raise ToolchainError(f"{name} must be fetched from its Galaxy artifact URL")
        if not isinstance(collection["sha256"], str) or not SHA256_RE.fullmatch(collection["sha256"]):
            raise ToolchainError(f"{name} sha256 must be 64 lowercase hex characters")
        required_by = collection.get("required_by")
        if required_by is not None and (
            not isinstance(required_by, list) or not required_by
            or not all(isinstance(item, str) and item in pinned_names for item in required_by)
        ):
            raise ToolchainError(f"{name} required_by must name pinned collections")
        names.append(name)
    if names != sorted(names) or len(set(names)) != len(names):
        raise ToolchainError("collections must be unique and sorted by name")

    kolla_collection = _exact_keys(manifest["kolla_collection"], KOLLA_COLLECTION_KEYS, "kolla_collection")
    if kolla_collection["name"] != "openstack.kolla" or kolla_collection["name"] in names:
        raise ToolchainError("kolla_collection must be openstack.kolla and only listed there")
    if kolla_collection["repository"] != KOLLA_COLLECTION_REPOSITORY:
        raise ToolchainError("kolla_collection.repository must be the OpenDev repository")
    if not COMMIT_RE.fullmatch(str(kolla_collection["commit"])):
        raise ToolchainError("kolla_collection.commit must be a full commit SHA")
    for field in ("release", "galaxy_version"):
        if not VERSION_RE.fullmatch(str(kolla_collection[field])):
            raise ToolchainError(f"kolla_collection.{field} must be x.y.z")

    return {
        **manifest,
        "requirements": entries,
        "files": {
            "toolchain_json_sha256": sha256_file(directory / "toolchain.json"),
            "requirements_lock_sha256": sha256_file(lock_path),
            "dockerfile_sha256": sha256_file(root / "toolchain" / "Dockerfile"),
            "fetch_collections_sha256": sha256_file(root / "toolchain" / "fetch-collections.py"),
        },
    }


def render_plan(
    version: str,
    candidate_id: str,
    *,
    root: Path = ROOT,
    base_manifest: bytes | Callable[[str], bytes] | None = None,
) -> dict[str, Any]:
    """Render the frozen plan: exact base children, tags and input digests."""
    if not CANDIDATE_RE.fullmatch(candidate_id):
        raise ToolchainError("candidate id must be <run id>-<run attempt>")
    toolchain = load_toolchain(version, root=root)
    matrix = load_json(root / "config" / "build-matrix.json")
    base_config = matrix["bases"][toolchain["base"]]
    resolved = resolve_base({**base_config, "id": toolchain["base"]}, base_manifest)
    repository = f"{matrix['registry']}/{matrix['owner']}/{matrix['repository']}/{IMAGE_NAME}"
    revision_tag = f"{version}-{base_config['distro']}-{base_config['os_version']}-rev-{candidate_id}"
    revision_ref = f"{repository}:{revision_tag}"
    architectures = {}
    for arch, runner in ARCH_RUNNERS.items():
        architectures[arch] = {
            "runner": runner,
            "platform": f"linux/{arch}",
            "base_ref": f"{base_config['image']}@{resolved['platforms'][arch]['digest']}",
            "revision_arch_ref": f"{revision_ref}-{arch}",
        }
    return {
        "schema_version": 1,
        "kind": "kolla-ansible-toolchain-plan",
        "toolchain": version,
        "release": toolchain["release"],
        "candidate_id": candidate_id,
        "kolla_ansible": toolchain["kolla_ansible"],
        "base": {key: resolved[key] for key in ("id", "requested_ref", "index_digest", "platforms")},
        "repository": repository,
        "revision_tag": revision_tag,
        "revision_ref": revision_ref,
        "architectures": architectures,
        "inputs": toolchain["files"],
    }


def load_plan(path: Path, *, root: Path = ROOT) -> dict[str, Any]:
    """Load a plan and prove it still matches the checked-out pins."""
    plan = load_json(path)
    if not isinstance(plan, dict) or plan.get("kind") != "kolla-ansible-toolchain-plan":
        raise ToolchainError("not a toolchain plan")
    toolchain = load_toolchain(plan["toolchain"], root=root)
    if plan["inputs"] != toolchain["files"]:
        raise ToolchainError("toolchain inputs changed after the plan was rendered")
    return plan


SMOKE_PROBE = r"""
import importlib.metadata as metadata
import json
import os
import subprocess


def run(*command):
    result = subprocess.run(command, capture_output=True, text=True, timeout=300)
    return {"returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr[-4000:]}


distributions = {}
for distribution in metadata.distributions():
    distributions[distribution.metadata["Name"]] = distribution.version
try:
    pbr = json.loads(metadata.distribution("kolla-ansible").read_text("pbr.json"))
except Exception:
    pbr = None
print(json.dumps({
    "uid": os.getuid(),
    "distributions": distributions,
    "kolla_ansible_pbr": pbr,
    "kolla_ansible_version": run("kolla-ansible", "--version"),
    "openstack_version": run("openstack", "--version"),
    "collections": run("ansible-galaxy", "collection", "list", "--format", "json"),
}))
"""


def check_smoke(toolchain: Mapping[str, Any], probe: Mapping[str, Any]) -> list[str]:
    """Compare a probe of the built image with the reviewed pins."""
    problems: list[str] = []
    if probe.get("uid") == 0:
        problems.append("image runs as root")
    installed = {normalize_name(name): version for name, version in probe.get("distributions", {}).items()}
    expected = {key: entry["version"] for key, entry in toolchain["requirements"].items()}
    if installed != expected:
        missing = sorted(set(expected) - set(installed))
        extra = sorted(set(installed) - set(expected))
        wrong = sorted(k for k in set(expected) & set(installed) if expected[k] != installed[k])
        problems.append(f"Python environment differs from requirements.lock: missing={missing} extra={extra} wrong={wrong}")
    pbr = probe.get("kolla_ansible_pbr") or {}
    git_version = pbr.get("git_version")
    commit = toolchain["kolla_ansible"]["commit"]
    if not isinstance(git_version, str) or len(git_version) < 7 or not commit.startswith(git_version):
        problems.append(f"kolla-ansible package was not built from {commit}")
    version_output = probe.get("kolla_ansible_version", {})
    if version_output.get("returncode") != 0 or toolchain["toolchain"] not in (
        version_output.get("stdout", "") + version_output.get("stderr", "")
    ):
        problems.append("kolla-ansible --version does not report the toolchain version")
    openstack = probe.get("openstack_version", {})
    client = toolchain["requirements"].get("python-openstackclient")
    if client and (
        openstack.get("returncode") != 0
        or client["version"] not in openstack.get("stdout", "") + openstack.get("stderr", "")
    ):
        problems.append("openstack --version does not report the pinned client")
    listing = probe.get("collections", {})
    found: dict[str, str] = {}
    if listing.get("returncode") == 0:
        try:
            for path, entries in json.loads(listing["stdout"]).items():
                for name, details in entries.items():
                    if name in found:
                        problems.append(f"collection {name} is installed twice")
                    found[name] = details.get("version")
        except (ValueError, AttributeError):
            problems.append("ansible-galaxy collection list output is not JSON")
    else:
        problems.append("ansible-galaxy collection list failed")
    wanted = {item["name"]: item["version"] for item in toolchain["collections"]}
    wanted[toolchain["kolla_collection"]["name"]] = toolchain["kolla_collection"]["galaxy_version"]
    if found != wanted:
        problems.append(f"installed collections differ: expected {wanted}, found {found}")
    return problems


def probe_image(image: str) -> dict[str, Any]:
    result = subprocess.run(
        ["docker", "run", "--rm", "--network", "none", "--entrypoint", f"{VENV}/bin/python3", image, "-c", SMOKE_PROBE],
        check=True,
        capture_output=True,
        text=True,
        timeout=600,
    )
    return json.loads(result.stdout)


def render_evidence(plan: Mapping[str, Any], arch: str, metadata: Mapping[str, Any]) -> dict[str, Any]:
    if arch not in plan["architectures"]:
        raise ToolchainError(f"unknown architecture: {arch}")
    digest = metadata.get("containerimage.digest")
    if not isinstance(digest, str) or not DIGEST_RE.fullmatch(digest):
        raise ToolchainError("build metadata has no image digest")
    planned = plan["architectures"][arch]
    return {
        "schema_version": 1,
        "kind": "kolla-ansible-toolchain-native",
        "candidate_id": plan["candidate_id"],
        "arch": arch,
        "platform": planned["platform"],
        "revision_arch_ref": planned["revision_arch_ref"],
        "digest": digest,
        "immutable_ref": f"{plan['repository']}@{digest}",
        "smoke": "passed",
    }


def render_lock(
    plan: Mapping[str, Any],
    evidence: Mapping[str, Mapping[str, Any]],
    raw_manifest: bytes,
    manifest_digest: str,
    source_commit: str,
    *,
    root: Path = ROOT,
) -> dict[str, Any]:
    """Bind the published multi-architecture manifest to the plan and pins."""
    if not DIGEST_RE.fullmatch(manifest_digest):
        raise ToolchainError("manifest digest must be sha256:<64 hex>")
    if "sha256:" + hashlib.sha256(raw_manifest).hexdigest() != manifest_digest:
        raise ToolchainError("raw manifest bytes do not match the manifest digest")
    if not COMMIT_RE.fullmatch(source_commit):
        raise ToolchainError("source commit must be a full SHA")
    manifest = json.loads(raw_manifest)
    if manifest.get("mediaType") not in EXPECTED_MANIFEST_MEDIA_TYPES:
        raise ToolchainError("published manifest is not a multi-architecture index")
    children = {
        f"{entry['platform']['os']}/{entry['platform']['architecture']}": entry["digest"]
        for entry in manifest.get("manifests", [])
    }
    expected = {}
    architectures = {}
    for arch in plan["architectures"]:
        record = evidence.get(arch)
        if record is None or record.get("candidate_id") != plan["candidate_id"] or record.get("arch") != arch:
            raise ToolchainError(f"missing or foreign {arch} evidence")
        if record.get("smoke") != "passed":
            raise ToolchainError(f"{arch} image did not pass its smoke check")
        expected[f"linux/{arch}"] = record["digest"]
        architectures[arch] = {
            "platform": f"linux/{arch}",
            "revision_arch_ref": record["revision_arch_ref"],
            "digest": record["digest"],
        }
    if children != expected:
        raise ToolchainError("published manifest children differ from the native evidence")
    toolchain = load_toolchain(plan["toolchain"], root=root)
    return {
        "schema_version": 1,
        "kind": "kolla-ansible-toolchain-lock",
        "toolchain": plan["toolchain"],
        "release": plan["release"],
        "candidate_id": plan["candidate_id"],
        "source_commit": source_commit,
        "image": {
            "repository": plan["repository"],
            "revision_ref": plan["revision_ref"],
            "manifest_digest": manifest_digest,
            "immutable_ref": f"{plan['repository']}@{manifest_digest}",
            "architectures": architectures,
        },
        "kolla_ansible": toolchain["kolla_ansible"],
        "python_requirements": {
            "upper_constraints_sha256": toolchain["python_requirements"]["upper_constraints_sha256"],
            "requirements_lock_sha256": toolchain["files"]["requirements_lock_sha256"],
            "packages": {entry["name"]: entry["version"] for entry in toolchain["requirements"].values()},
        },
        "collections": toolchain["collections"],
        "kolla_collection": toolchain["kolla_collection"],
        "base": plan["base"],
        "inputs": plan["inputs"],
    }


def _write_json(path: Path | None, payload: Any) -> None:
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if path is None:
        sys.stdout.write(text)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)

    validate = commands.add_parser("validate", help="validate every reviewed toolchain pin set")
    validate.add_argument("--toolchain", action="append")

    plan = commands.add_parser("plan", help="render the frozen build plan")
    plan.add_argument("--toolchain", required=True)
    plan.add_argument("--candidate-id", required=True)
    plan.add_argument("--base-manifest", type=Path, help="raw base index for offline rendering")
    plan.add_argument("--output", type=Path)

    native = commands.add_parser("native", help="print one architecture's build inputs for GITHUB_OUTPUT")
    native.add_argument("--plan", type=Path, required=True)
    native.add_argument("--arch", required=True, choices=sorted(ARCH_RUNNERS))

    smoke = commands.add_parser("smoke", help="check a built image against the pins")
    smoke.add_argument("--plan", type=Path, required=True)
    smoke.add_argument("--image", required=True)

    evidence = commands.add_parser("evidence", help="record one smoke-checked native image")
    evidence.add_argument("--plan", type=Path, required=True)
    evidence.add_argument("--arch", required=True, choices=sorted(ARCH_RUNNERS))
    evidence.add_argument("--metadata-file", type=Path, required=True)
    evidence.add_argument("--output", type=Path, required=True)

    lock = commands.add_parser("lock", help="write the toolchain lock for a published manifest")
    lock.add_argument("--plan", type=Path, required=True)
    lock.add_argument("--evidence-dir", type=Path, required=True)
    lock.add_argument("--manifest-metadata-file", type=Path, required=True)
    lock.add_argument("--source-commit", required=True)
    lock.add_argument("--raw-manifest-output", type=Path, required=True)
    lock.add_argument("--output", type=Path, required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            versions = args.toolchain or toolchain_versions()
            if not versions:
                raise ToolchainError("no toolchain pin sets under toolchain/")
            for version in versions:
                load_toolchain(version)
                print(f"toolchain {version}: pins valid")
        elif args.command == "plan":
            raw = args.base_manifest.read_bytes() if args.base_manifest else None
            _write_json(args.output, render_plan(args.toolchain, args.candidate_id, base_manifest=raw))
        elif args.command == "native":
            planned = load_plan(args.plan)["architectures"][args.arch]
            for key in ("platform", "base_ref", "revision_arch_ref"):
                print(f"{key}={planned[key]}")
        elif args.command == "smoke":
            planned = load_plan(args.plan)
            problems = check_smoke(load_toolchain(planned["toolchain"]), probe_image(args.image))
            for problem in problems:
                print(f"smoke: {problem}", file=sys.stderr)
            if problems:
                return 1
            print(f"smoke: {args.image} matches toolchain {planned['toolchain']}")
        elif args.command == "evidence":
            planned = load_plan(args.plan)
            _write_json(args.output, render_evidence(planned, args.arch, load_json(args.metadata_file)))
        elif args.command == "lock":
            planned = load_plan(args.plan)
            records = {}
            for path in sorted(args.evidence_dir.glob("*.json")):
                record = load_json(path)
                records[record["arch"]] = record
            metadata = load_json(args.manifest_metadata_file)
            digest = metadata.get("containerimage.digest") or metadata.get("containerimage.descriptor", {}).get("digest")
            if not isinstance(digest, str) or not DIGEST_RE.fullmatch(digest):
                raise ToolchainError("manifest metadata has no digest")
            raw = inspect_raw_manifest(f"{planned['repository']}@{digest}")
            if inspect_raw_manifest(planned["revision_ref"]) != raw:
                raise ToolchainError("revision tag moved while the lock was written")
            payload = render_lock(planned, records, raw, digest, args.source_commit)
            args.raw_manifest_output.parent.mkdir(parents=True, exist_ok=True)
            args.raw_manifest_output.write_bytes(raw)
            _write_json(args.output, payload)
    except (ToolchainError, KeyError, ValueError, subprocess.CalledProcessError) as error:
        print(f"toolchain: {error}", file=sys.stderr)
        return 1
    return 0


# Pin generation (manual maintenance; not part of an image build).


def _fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "kolla-container-images"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def _requirements_core(commit: str) -> list[tuple[str, str]]:
    """Collections and constraints from Kolla-Ansible's requirements-core.yml."""
    text = _fetch(
        f"https://opendev.org/openstack/kolla-ansible/raw/commit/{commit}/requirements-core.yml"
    ).decode()
    result = []
    name = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("- name:"):
            name = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("version:") and name:
            result.append((name, stripped.split(":", 1)[1].strip().strip("'\"")))
            name = None
    if not result:
        raise ToolchainError("requirements-core.yml lists no collections")
    return result


def _galaxy_pin(name: str, constraint: str) -> tuple[dict[str, Any], dict[str, str]]:
    namespace, short = name.split(".")
    url: str | None = f"{GALAXY_INDEX}/{namespace}/{short}/versions/?limit=100"
    versions = []
    while url:
        page = json.loads(_fetch(url))
        versions += [item["version"] for item in page["data"]]
        following = page.get("links", {}).get("next")
        url = f"https://galaxy.ansible.com{following}" if following else None
    stable = [v for v in versions if VERSION_RE.fullmatch(v) and satisfies(v, constraint)]
    if not stable:
        raise ToolchainError(f"no Galaxy release of {name} satisfies {constraint}")
    version = max(stable, key=_version_key)
    details = json.loads(_fetch(f"{GALAXY_INDEX}/{namespace}/{short}/versions/{version}/"))
    pin = {
        "name": name,
        "constraint": constraint,
        "version": version,
        "url": f"{GALAXY_ARTIFACTS}{namespace}-{short}-{version}.tar.gz",
        "sha256": details["artifact"]["sha256"],
    }
    if details["download_url"] != pin["url"]:
        raise ToolchainError(f"Galaxy download URL for {name} {version} is not the artifact URL")
    return pin, details.get("metadata", {}).get("dependencies") or {}


def _kolla_collection(releases_commit: str, series: str) -> dict[str, str]:
    text = _fetch(
        "https://opendev.org/openstack/releases/raw/commit/"
        f"{releases_commit}/deliverables/{series}/ansible-collection-kolla.yaml"
    ).decode()
    releases = re.findall(r"- version: ([0-9.]+)\s+projects:\s+- repo: openstack/ansible-collection-kolla\s+hash: ([0-9a-f]{40})", text)
    if not releases:
        raise ToolchainError(f"no ansible-collection-kolla release in {series}")
    release, commit = releases[-1]
    galaxy = _fetch(f"{KOLLA_COLLECTION_REPOSITORY}/raw/commit/{commit}/galaxy.yml").decode()
    match = re.search(r"(?m)^version:\s*([0-9.]+)\s*$", galaxy)
    if not match:
        raise ToolchainError("galaxy.yml has no version")
    return {
        "name": "openstack.kolla",
        "release": release,
        "repository": KOLLA_COLLECTION_REPOSITORY,
        "commit": commit,
        "galaxy_version": match.group(1),
    }


def _compile_lock(inputs: list[str], constraints: Path, platform: str) -> str:
    with tempfile.TemporaryDirectory() as temporary:
        source = Path(temporary) / "requirements.in"
        source.write_text("\n".join(inputs) + "\n", encoding="utf-8")
        result = subprocess.run(
            [
                "uv", "pip", "compile", "--quiet", "--python-version", "3.12",
                "--python-platform", platform, "--generate-hashes", "--no-annotate",
                "--no-header", "--constraint", str(constraints), str(source),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    return result.stdout


def generate_pins(version: str, *, root: Path = ROOT) -> None:
    """Resolve Galaxy, OpenDev and PyPI pins for one toolchain and write them."""
    matrix = load_json(root / "config" / "build-matrix.json")
    commit = matrix["toolchains"][version]["kolla_ansible"]["commit"]
    stream = next(s for s in matrix["streams"] if s["toolchain"] == version and s["publish_enabled"])
    release = stream["release"]
    series = matrix["releases"][release]["series"]
    source_set = load_json(SOURCE_SET_ROOT / f"{matrix['releases'][release]['source_set']}.json")
    requirements = source_set["projects"]["openstack/requirements"]
    constraints_bytes = _fetch(
        f"{requirements['repository']}/raw/commit/{requirements['build_commit']}/upper-constraints.txt"
    )
    if hashlib.sha256(constraints_bytes).hexdigest() != requirements["upper_constraints_sha256"]:
        raise ToolchainError("downloaded upper-constraints differ from the source-set digest")

    pins: dict[str, dict[str, Any]] = {}
    pending = [(name, constraint, None) for name, constraint in _requirements_core(commit)]
    while pending:
        name, constraint, parent = pending.pop(0)
        if name in pins:
            if parent:
                pins[name].setdefault("required_by", []).append(parent)
            continue
        pin, dependencies = _galaxy_pin(name, constraint)
        if parent:
            pin["required_by"] = [parent]
        pins[name] = pin
        pending += [(dep, spec.replace(" ", ""), name) for dep, spec in dependencies.items()]

    inputs = [
        f"kolla-ansible=={version}",
        "ansible-core>=2.19,!=2.19.0,<2.21",
        "python-openstackclient",
        "python-octaviaclient",
    ]
    directory = root / "toolchain" / version
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary:
        constraints = Path(temporary) / "upper-constraints.txt"
        constraints.write_bytes(constraints_bytes)
        amd64 = _compile_lock(inputs, constraints, "x86_64-manylinux_2_39")
        arm64 = _compile_lock(inputs, constraints, "aarch64-manylinux_2_39")
    if amd64 != arm64:
        raise ToolchainError("amd64 and arm64 resolutions differ; review before pinning")
    header = (
        f"# Generated by scripts/generate-toolchain-pins.py for Kolla-Ansible {version} on CPython 3.12.\n"
        "# Resolution verified identical for x86_64-manylinux_2_39 and aarch64-manylinux_2_39\n"
        f"# under upper-constraints {requirements['upper_constraints_sha256']}.\n"
        f"# Direct inputs: {', '.join(inputs)}.\n"
    )
    (directory / "requirements.lock").write_text(header + amd64, encoding="utf-8")
    manifest = {
        "schema_version": 1,
        "toolchain": version,
        "release": release,
        "base": "ubuntu-24.04",
        "kolla_ansible": {"version": version, "commit": commit},
        "python_requirements": {
            "inputs": inputs,
            "upper_constraints_sha256": requirements["upper_constraints_sha256"],
            "lock": "requirements.lock",
        },
        "collections": [pins[name] for name in sorted(pins)],
        "kolla_collection": _kolla_collection(matrix["release_metadata"]["commit"], series),
    }
    (directory / "toolchain.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    load_toolchain(version, root=root)


def generator_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Resolve and write the pins for one toolchain image.")
    parser.add_argument("--toolchain", required=True)
    args = parser.parse_args(argv)
    try:
        generate_pins(args.toolchain)
    except (ToolchainError, KeyError, StopIteration, subprocess.CalledProcessError) as error:
        print(f"toolchain pins: {error}", file=sys.stderr)
        return 1
    print(f"wrote toolchain/{args.toolchain}/toolchain.json and requirements.lock")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
