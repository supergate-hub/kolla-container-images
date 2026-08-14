#!/usr/bin/env python3
"""Validate frozen Kolla build argv against every pinned upstream parser."""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

try:
    from scripts.frozen_sources import (
        FrozenSourceError,
        checkout_exact_repository,
        verify_exact_checkout,
    )
    from scripts.profile_resolver import (
        find_stream,
        load_matrix,
        load_profile,
        resolve_profile,
    )
except ModuleNotFoundError:
    from frozen_sources import (
        FrozenSourceError,
        checkout_exact_repository,
        verify_exact_checkout,
    )
    from profile_resolver import (
        find_stream,
        load_matrix,
        load_profile,
        resolve_profile,
    )


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = Path(__file__).resolve()
PLAN_SCRIPT_PATH = ROOT / "scripts" / "plan-publish.py"
DEFAULT_MATRIX_PATH = ROOT / "config" / "build-matrix.json"
DEFAULT_BASE_MANIFEST_PATH = ROOT / "tests" / "fixtures" / "oci-base-index.json"
TOOLCHAIN_VERSION_RE = re.compile(
    r"^[1-9][0-9]*\.[0-9]+\.[0-9]+(?:\.[0-9A-Za-z]+)*$"
)
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
TARGET_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

PlanProvider = Callable[[str, dict[str, Any]], dict[str, Any]]


class KollaCliContractError(ValueError):
    """Raised when a pinned Kolla parser rejects the frozen command contract."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise KollaCliContractError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _load_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise KollaCliContractError(
            f"cannot read JSON object {path}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise KollaCliContractError(f"JSON document must be an object: {path}")
    return value


def representative_toolchains(
    matrix: dict[str, Any],
) -> list[tuple[str, dict[str, Any], dict[str, str]]]:
    """Return one deterministic representative stream for every toolchain."""
    toolchains = matrix.get("toolchains")
    streams = matrix.get("streams")
    if not isinstance(toolchains, dict) or not toolchains:
        raise KollaCliContractError("matrix toolchains must be a non-empty object")
    if not isinstance(streams, list) or not streams:
        raise KollaCliContractError("matrix streams must be a non-empty list")
    if any(
        not isinstance(version, str) or not TOOLCHAIN_VERSION_RE.fullmatch(version)
        for version in toolchains
    ):
        raise KollaCliContractError("matrix toolchain keys must be Kolla versions")

    stream_ids: set[str] = set()
    streams_by_toolchain: dict[str, list[dict[str, Any]]] = {
        version: [] for version in toolchains
    }
    for index, stream in enumerate(streams):
        if not isinstance(stream, dict):
            raise KollaCliContractError(f"matrix streams[{index}] must be an object")
        stream_id = stream.get("id")
        version = stream.get("toolchain")
        if not isinstance(stream_id, str) or not stream_id:
            raise KollaCliContractError(
                f"matrix streams[{index}].id must be a non-empty string"
            )
        if stream_id in stream_ids:
            raise KollaCliContractError(f"duplicate matrix stream ID: {stream_id!r}")
        stream_ids.add(stream_id)
        if not isinstance(version, str) or version not in toolchains:
            raise KollaCliContractError(
                f"matrix stream {stream_id!r} references an unknown toolchain"
            )
        streams_by_toolchain[version].append(stream)

    representatives: list[tuple[str, dict[str, Any], dict[str, str]]] = []
    for version in sorted(toolchains):
        matching = streams_by_toolchain[version]
        if not matching:
            raise KollaCliContractError(
                f"matrix toolchain {version!r} has no representative stream"
            )
        toolchain = toolchains[version]
        if not isinstance(toolchain, dict):
            raise KollaCliContractError(
                f"matrix toolchain {version!r} must be an object"
            )
        kolla = toolchain.get("kolla")
        if not isinstance(kolla, dict):
            raise KollaCliContractError(
                f"matrix toolchain {version!r} Kolla pin must be an object"
            )
        repository = kolla.get("repository")
        commit = kolla.get("commit")
        if not isinstance(repository, str) or not repository:
            raise KollaCliContractError(
                f"matrix toolchain {version!r} Kolla repository is invalid"
            )
        if not isinstance(commit, str) or not COMMIT_RE.fullmatch(commit):
            raise KollaCliContractError(
                f"matrix toolchain {version!r} Kolla commit is invalid"
            )
        representative = min(matching, key=lambda item: item["id"])
        representatives.append(
            (
                version,
                representative,
                {"repository": repository, "commit": commit},
            )
        )
    return representatives


def _plan_unit(
    plan: dict[str, Any],
    *,
    stream_id: str,
    target: str,
) -> tuple[str, list[str]]:
    if plan.get("stream") != stream_id:
        raise KollaCliContractError(
            f"representative plan stream does not match {stream_id!r}"
        )
    build = plan.get("build")
    units = build.get("all_units") if isinstance(build, dict) else None
    if not isinstance(units, list):
        raise KollaCliContractError("representative plan has no build units")
    matches = [
        unit
        for unit in units
        if isinstance(unit, dict)
        and unit.get("arch") == "amd64"
        and unit.get("kind") == "leaf"
        and unit.get("target") == target
    ]
    if len(matches) != 1:
        raise KollaCliContractError(
            f"representative plan must contain one amd64 leaf unit for {target!r}"
        )
    unit = matches[0]
    unit_id = unit.get("id")
    if not isinstance(unit_id, str) or not unit_id:
        raise KollaCliContractError("representative plan unit ID is invalid")
    command = unit.get("command")
    if (
        not isinstance(command, list)
        or len(command) < 2
        or command[0] != "kolla-build"
        or any(not isinstance(part, str) or not part for part in command)
    ):
        raise KollaCliContractError("representative frozen Kolla command is invalid")
    expected_regex = f"^{target}$"
    if command[-1] != expected_regex:
        raise KollaCliContractError(
            f"frozen command must end with exact target regex {expected_regex!r}"
        )
    return unit_id, command


def _option_value(command: list[str], option: str, *, required: bool) -> str | None:
    positions = [index for index, value in enumerate(command) if value == option]
    if not positions:
        if required:
            raise KollaCliContractError(
                f"frozen Kolla command must contain exactly one {option}"
            )
        return None
    if len(positions) != 1 or positions[0] + 1 >= len(command):
        raise KollaCliContractError(
            f"frozen Kolla command must contain exactly one value for {option}"
        )
    value = command[positions[0] + 1]
    if value.startswith("--"):
        raise KollaCliContractError(f"frozen Kolla command {option} value is missing")
    return value


def _safe_output_path(workdir: Path, raw_path: str, *, context: str) -> Path:
    relative = Path(raw_path)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise KollaCliContractError(
            f"{context} must be a safe relative path: {raw_path!r}"
        )
    output = workdir.joinpath(relative)
    try:
        output.resolve().relative_to(workdir.resolve())
    except ValueError as error:
        raise KollaCliContractError(
            f"{context} escapes the parser work directory: {raw_path!r}"
        ) from error
    return output


def _frozen_file(
    plan: dict[str, Any],
    name: str,
) -> tuple[str, str]:
    sources = plan.get("openstack_sources")
    value = sources.get(name) if isinstance(sources, dict) else None
    if not isinstance(value, dict):
        raise KollaCliContractError(f"plan frozen source {name} is invalid")
    content = value.get("content")
    expected_digest = value.get("sha256")
    if not isinstance(content, str) or not isinstance(expected_digest, str):
        raise KollaCliContractError(f"plan frozen source {name} is incomplete")
    actual_digest = "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()
    if actual_digest != expected_digest:
        raise KollaCliContractError(
            f"plan frozen source {name} digest does not match its content"
        )
    return content, actual_digest


def materialize_parser_inputs(
    plan: dict[str, Any],
    command: list[str],
    workdir: Path,
) -> None:
    """Write frozen config bytes to the exact relative paths recorded in argv."""
    config_path = _option_value(command, "--config-file", required=True)
    assert config_path is not None
    config_content, _ = _frozen_file(plan, "kolla_build_config")
    config_output = _safe_output_path(
        workdir,
        config_path,
        context="frozen Kolla config path",
    )
    config_output.parent.mkdir(parents=True, exist_ok=True)
    config_output.write_bytes(config_content.encode("utf-8"))

    override_content, _ = _frozen_file(plan, "template_override")
    override_path = _option_value(
        command,
        "--template-override",
        required=bool(override_content),
    )
    if not override_content:
        if override_path is not None:
            raise KollaCliContractError(
                "frozen Kolla command must not reference an empty template override"
            )
        return
    assert override_path is not None
    override_output = _safe_output_path(
        workdir,
        override_path,
        context="frozen Kolla template override path",
    )
    override_output.parent.mkdir(parents=True, exist_ok=True)
    override_output.write_bytes(override_content.encode("utf-8"))


def _run_parser_worker(
    *,
    plan: dict[str, Any],
    unit_id: str,
    checkout: Path,
    toolchain_version: str,
    workdir: Path,
    python_executable: Path,
    worker_script: Path,
) -> None:
    contract_path = workdir / "parser-contract.json"
    contract_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "toolchain": toolchain_version,
                "checkout": str(checkout.resolve()),
                "unit_id": unit_id,
                "plan": plan,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(checkout.resolve())
    environment["PBR_VERSION"] = toolchain_version
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [
            str(python_executable),
            str(worker_script.resolve()),
            "__parser-worker",
            "--contract",
            str(contract_path),
        ],
        cwd=workdir,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
        raise KollaCliContractError(
            f"toolchain {toolchain_version!r} parser contract failed: {detail}"
        )
    try:
        result = json.loads(completed.stdout, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as error:
        raise KollaCliContractError(
            f"toolchain {toolchain_version!r} parser worker returned invalid JSON"
        ) from error
    expected = {
        "toolchain": toolchain_version,
        "unit_id": unit_id,
        "validated": True,
    }
    if result != expected:
        raise KollaCliContractError(
            f"toolchain {toolchain_version!r} parser worker result is invalid"
        )


def validate_contract(
    matrix: dict[str, Any],
    *,
    plan_provider: PlanProvider,
    checkout_root: Path,
    python_executable: Path,
    worker_script: Path = SCRIPT_PATH,
    target: str = "keystone",
) -> list[dict[str, str]]:
    """Validate all unique toolchains with isolated checkouts and parsers."""
    if not TARGET_RE.fullmatch(target):
        raise KollaCliContractError(f"Kolla target is invalid: {target!r}")
    if checkout_root.exists():
        if checkout_root.is_symlink() or not checkout_root.is_dir():
            raise KollaCliContractError("checkout root must be a real directory")
        if any(checkout_root.iterdir()):
            raise KollaCliContractError("checkout root must be empty")
    else:
        checkout_root.mkdir(parents=True)

    results: list[dict[str, str]] = []
    for version, stream, kolla_pin in representative_toolchains(matrix):
        stream_id = stream["id"]
        plan = plan_provider(version, stream)
        if not isinstance(plan, dict):
            raise KollaCliContractError(
                f"representative plan for {stream_id!r} must be an object"
            )
        unit_id, command = _plan_unit(
            plan,
            stream_id=stream_id,
            target=target,
        )

        checkout = checkout_root / f"kolla-{version}"
        try:
            checkout_exact_repository(
                checkout,
                repository=kolla_pin["repository"],
                commit=kolla_pin["commit"],
            )
            verify_exact_checkout(
                checkout,
                repository=kolla_pin["repository"],
                commit=kolla_pin["commit"],
            )
        except FrozenSourceError as error:
            raise KollaCliContractError(
                f"toolchain {version!r} exact Kolla checkout failed: {error}"
            ) from error

        workdir = checkout_root / f"parser-{version}"
        workdir.mkdir()
        materialize_parser_inputs(plan, command, workdir)
        frozen_plan = copy.deepcopy(plan)
        _run_parser_worker(
            plan=frozen_plan,
            unit_id=unit_id,
            checkout=checkout,
            toolchain_version=version,
            workdir=workdir,
            python_executable=python_executable,
            worker_script=worker_script,
        )
        try:
            verify_exact_checkout(
                checkout,
                repository=kolla_pin["repository"],
                commit=kolla_pin["commit"],
            )
        except FrozenSourceError as error:
            raise KollaCliContractError(
                f"toolchain {version!r} parser dirtied its Kolla checkout: {error}"
            ) from error
        if plan != frozen_plan:
            raise KollaCliContractError("parser validation mutated the frozen plan")
        results.append(
            {
                "toolchain": version,
                "stream": stream_id,
                "target_regex": command[-1],
            }
        )
    return results


def _load_plan_module() -> Any:
    scripts_dir = str(PLAN_SCRIPT_PATH.parent)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    spec = importlib.util.spec_from_file_location(
        "_kolla_cli_contract_plan_publish",
        PLAN_SCRIPT_PATH,
    )
    if spec is None or spec.loader is None:
        raise KollaCliContractError("cannot load the repository publish planner")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate_repository_contract(
    *,
    matrix_path: Path,
    profile_name: str,
    image: str,
    base_manifest_path: Path,
    checkout_root: Path,
    python_executable: Path = Path(sys.executable),
) -> list[dict[str, str]]:
    try:
        matrix = load_matrix(matrix_path)
        profile = load_profile(profile_name, matrix_path.parent / "profiles")
        base_manifest = base_manifest_path.read_bytes()
        planner = _load_plan_module()
    except (OSError, ValueError) as error:
        raise KollaCliContractError(
            f"cannot prepare repository publish plans: {error}"
        ) from error

    def plan_provider(_version: str, raw_stream: dict[str, Any]) -> dict[str, Any]:
        try:
            stream = find_stream(matrix, raw_stream["id"])
            resolved_profile = resolve_profile(profile, stream)
            return planner.build_plan(
                matrix,
                resolved_profile,
                stream,
                image_filter=image,
                base_manifest=base_manifest,
            )
        except (OSError, ValueError) as error:
            raise KollaCliContractError(
                f"cannot render representative plan for {raw_stream['id']!r}: {error}"
            ) from error

    return validate_contract(
        matrix,
        plan_provider=plan_provider,
        checkout_root=checkout_root,
        python_executable=python_executable,
        target=image,
    )


def _parser_worker(contract_path: Path) -> int:
    contract = _load_json_object(contract_path)
    if set(contract) != {"schema_version", "toolchain", "checkout", "unit_id", "plan"}:
        raise KollaCliContractError("parser worker contract keys are invalid")
    if contract.get("schema_version") != 1:
        raise KollaCliContractError("parser worker contract schema is invalid")
    toolchain = contract.get("toolchain")
    checkout_value = contract.get("checkout")
    unit_id = contract.get("unit_id")
    plan = contract.get("plan")
    if not isinstance(toolchain, str) or not TOOLCHAIN_VERSION_RE.fullmatch(toolchain):
        raise KollaCliContractError("parser worker toolchain is invalid")
    if not isinstance(checkout_value, str) or not isinstance(unit_id, str):
        raise KollaCliContractError("parser worker checkout or unit ID is invalid")
    if not isinstance(plan, dict):
        raise KollaCliContractError("parser worker plan is invalid")
    checkout = Path(checkout_value).resolve()
    if os.environ.get("PYTHONPATH") != str(checkout):
        raise KollaCliContractError(
            "parser worker PYTHONPATH is not the exact checkout"
        )
    if os.environ.get("PBR_VERSION") != toolchain:
        raise KollaCliContractError("parser worker PBR_VERSION is not the toolchain")

    import kolla

    raw_package_file = getattr(kolla, "__file__", None)
    if not isinstance(raw_package_file, str):
        raise KollaCliContractError("imported Kolla package has no source path")
    package_file = Path(raw_package_file).resolve()
    try:
        package_file.relative_to(checkout)
    except ValueError as error:
        raise KollaCliContractError(
            f"Kolla parser was imported outside the exact checkout: {package_file}"
        ) from error

    try:
        from frozen_sources import verify_kolla_build_command
    except ModuleNotFoundError:
        from scripts.frozen_sources import verify_kolla_build_command

    before = copy.deepcopy(plan)
    verify_kolla_build_command(plan, unit_id)
    if plan != before:
        raise KollaCliContractError("upstream Kolla parser mutated the frozen plan")
    print(
        json.dumps(
            {"toolchain": toolchain, "unit_id": unit_id, "validated": True},
            sort_keys=True,
        )
    )
    return 0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Validate repository frozen build argv against every exact pinned "
            "upstream Kolla parser"
        ),
        allow_abbrev=False,
    )
    parser.add_argument("--matrix", type=Path, default=DEFAULT_MATRIX_PATH)
    parser.add_argument("--profile", default="core")
    parser.add_argument("--image", default="keystone")
    parser.add_argument(
        "--base-manifest",
        type=Path,
        default=DEFAULT_BASE_MANIFEST_PATH,
        help="Raw OCI base index used only to render deterministic frozen plans",
    )
    parser.add_argument(
        "--checkout-root",
        type=Path,
        help="Optional empty directory retained after validation for local inspection",
    )
    return parser.parse_args(argv)


def _parse_worker_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--contract", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    try:
        if raw_argv[:1] == ["__parser-worker"]:
            worker_args = _parse_worker_args(raw_argv[1:])
            return _parser_worker(worker_args.contract)

        args = parse_args(raw_argv)
        if args.checkout_root is None:
            with tempfile.TemporaryDirectory(prefix="kolla-cli-contract-") as temp_dir:
                results = validate_repository_contract(
                    matrix_path=args.matrix,
                    profile_name=args.profile,
                    image=args.image,
                    base_manifest_path=args.base_manifest,
                    checkout_root=Path(temp_dir),
                )
        else:
            results = validate_repository_contract(
                matrix_path=args.matrix,
                profile_name=args.profile,
                image=args.image,
                base_manifest_path=args.base_manifest,
                checkout_root=args.checkout_root,
            )
    except (KollaCliContractError, FrozenSourceError) as error:
        print(f"Kolla CLI contract validation failed: {error}", file=sys.stderr)
        return 2

    for result in results:
        print(
            "Validated Kolla CLI contract: "
            f"toolchain={result['toolchain']} stream={result['stream']} "
            f"target_regex={result['target_regex']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
