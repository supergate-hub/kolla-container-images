# Kolla Multi-Stream Organization GHCR Implementation Plan

> **Supersession note (2026-07-14):** Current execution follows
> [Kolla Publish Hardening Design](../specs/2026-07-14-publish-hardening-design.md).
> Candidate-qualified refs now feed locks; the stable stream tag is a
> post-artifact convenience alias only. Historical `workflow_call`
> requirements below are superseded: CI creates a separate
> `workflow_dispatch` run to preserve run-derived candidate identity and its
> run-scoped artifact namespace.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the personal GHCR namespace with the organization namespace and deliver a safe, stream-aware dry-run and approval-gated Kolla image supply pipeline for seven OpenStack/base-OS streams and two native architectures.

**Architecture:** `config/build-matrix.json` defines seven explicit streams. A standard-library resolver turns schema-v3 `core` and `deployment` catalogs into one exact stream-specific profile consumed unchanged by planning, summary validation, approval, and candidate-lock generation. GitHub Actions plans read-only by default, permits only three explicitly approved publication scopes, builds once per native architecture, and stops at an environment-neutral candidate lock.

**Tech Stack:** Python 3 standard library, JSON, Kolla/Kolla-Ansible 20.4.0/21.1.0/22.0.0, GitHub Actions YAML, Docker/Buildx, GHCR, `unittest`, `actionlint`.

## Global Constraints

- Keep the current branch name unchanged.
- Registry identity is exactly `ghcr.io/supergate-hub/kolla-container-images`.
- Supported streams are exactly `2025.1-rocky-9`, `2025.1-rocky-10`, `2025.1-ubuntu-noble`, `2025.2-rocky-10`, `2025.2-ubuntu-noble`, `2026.1-rocky-10`, and `2026.1-ubuntu-noble`.
- Ubuntu uses `24.04` as the Kolla base tag and `noble` in deploy references.
- Every resolved leaf must produce native `amd64` and `arm64` refs and one architecture-neutral multi-architecture manifest.
- QEMU evidence never satisfies ARM64 build, image-smoke, or deployment-smoke readiness.
- `deployment` resolves to 63/64 leaves for 2025.1 and 2025.2 Rocky/Ubuntu, and 65/66 leaves for 2026.1 Rocky/Ubuntu.
- Include Cinder, Manila, Octavia, Valkey, Prometheus, Grafana, Fluentd, OpenSearch, and OpenSearch Dashboards; exclude Ceph daemons, etcd, multipathd, Redis, Designate, Swift, and Ironic.
- `tgtd` applies only to Ubuntu; the two new Prometheus exporters apply only to 2026.1.
- All seven streams are publication-capable, but every non-dry-run requires an exact scope variable, count-bearing approval phrase, and protected GitHub environment approval.
- Real publication scopes are limited to `core/keystone`, `core/all`, and `deployment/all`.
- Only `deployment/all` may produce a generic candidate lock.
- Do not add Dev/Stg/Prod locks, tags, pointers, promotion, deployment, rollback, or environment-specific state.
- Do not perform a GHCR push, workflow dispatch, repository-variable mutation, package-visibility mutation, push, or PR creation.
- Do not add external Python packages or unrelated refactors.
- Preserve the user's requirement for one final local commit: no worker or subagent may commit after an individual task; only the root agent creates the single final commit after all verification passes.
- Use `apply_patch` for source and documentation edits.
- Reference design: `docs/superpowers/specs/2026-07-13-kolla-multi-stream-ghcr-design.md`.

## File Structure

- Create `scripts/profile_resolver.py`: shared matrix lookup, selector matching, profile loading/resolution, and tag rendering.
- Create `tests/test_namespace_transition.py`: organization identity and personal-namespace absence.
- Create `tests/test_profile_resolution.py`: stream selection, conditional resolution, and fail-closed selectors.
- Create `tests/test_config_validation.py`: exact matrix/profile policy validation.
- Modify `config/build-matrix.json`: organization owner, seven streams, pinned toolchains, and two architectures.
- Modify `config/profiles/core.json`: schema v3, reviewed streams, and release-aware Neutron aliases.
- Modify `config/profiles/deployment.json`: schema v3 and the Cinder/Manila/Valkey/runtime/monitoring closure.
- Modify `scripts/validate-config.py`: validate matrix v2, raw profile v3, and every resolved stream.
- Modify `scripts/plan-publish.py`: accept `--stream` and emit two native build units, refs, evidence commands, summary/lock paths, and approval metadata.
- Modify `scripts/validate-publish-summary.py`: validate stream-derived scope and exact resolved coverage.
- Modify `scripts/generate-lock.py`: generate only a complete `deployment/all` lock.
- Create `scripts/publish_approval.py`: share allowed-scope, variable, count, and exact-phrase derivation between plan and validator.
- Modify `scripts/validate-publish-approval.py`: validate a frozen plan against three scope variables and an exact phrase.
- Modify `.github/workflows/publish.yml`: reusable/manual inputs, read-only default, protected authorization, two native builds, and manifest validation.
- Modify `.github/workflows/validate.yml`: validate all JSON and representative dry-run plans.
- Modify all seven existing test modules to consume stream IDs and resolved profiles.
- Modify `README.md`, `docs/publish.md`, and `docs/build-readiness.md`: final operator and handoff contracts.

---

### Task 1: Record Baseline and Prove the Namespace Transition RED/GREEN

**Files:**

- Create: `tests/test_namespace_transition.py`
- Modify: `config/build-matrix.json`
- Modify: `scripts/validate-publish-approval.py`
- Modify: `tests/test_deployment_profile.py`
- Modify: `tests/test_lock_generation.py`
- Modify: `tests/test_plan_publish.py`
- Modify: `tests/test_publish_approval.py`
- Modify: `tests/test_publish_summary_validation.py`
- Modify: `README.md`
- Modify: `docs/publish.md`
- Modify: `docs/build-readiness.md`

**Interfaces:**

- Consumes: current matrix owner and planner/lock/approval outputs.
- Produces: a repository-wide invariant that code, tests, and docs use `supergate-hub/kolla-container-images`.

- [ ] **Step 1: Reconfirm the clean baseline before test edits**

Run:

```bash
git status --short --branch
git diff origin/main...
python3 -m unittest discover -s tests -v 2>&1 | tee .context/baseline-tests.txt
```

Expected: branch name unchanged, only approved spec/plan documents untracked, no source diff from `origin/main`, and `Ran 49 tests` followed by `OK`.

- [ ] **Step 2: Add the failing organization-namespace test**

Create `tests/test_namespace_transition.py`:

```python
from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_OWNER = "supergate-hub"
EXPECTED_NAMESPACE = "supergate-hub/kolla-container-images"
PERSONAL_OWNER = "supergate-" + "jhbyun"
SKIP_PARTS = {".git", ".context", "__pycache__"}


class NamespaceTransitionTest(unittest.TestCase):
    def test_matrix_uses_organization_owner(self) -> None:
        matrix = json.loads(
            (ROOT / "config" / "build-matrix.json").read_text(encoding="utf-8")
        )
        self.assertEqual(matrix["owner"], EXPECTED_OWNER)
        self.assertEqual(
            f'{matrix["owner"]}/{matrix["repository"]}',
            EXPECTED_NAMESPACE,
        )

    def test_personal_owner_is_absent_from_repository_content(self) -> None:
        matches: list[str] = []
        for path in ROOT.rglob("*"):
            if not path.is_file() or any(part in SKIP_PARTS for part in path.parts):
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            if PERSONAL_OWNER in content:
                matches.append(str(path.relative_to(ROOT)))
        self.assertEqual(matches, [])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run the namespace test and capture RED**

Run:

```bash
python3 -m unittest tests/test_namespace_transition.py -v 2>&1 | tee .context/red-namespace.txt
```

Expected: FAIL because the matrix still has the personal owner and existing content still references it.

- [ ] **Step 4: Apply the minimal namespace conversion**

Use `apply_patch` to set the matrix owner to `supergate-hub` and replace all personal-namespace expectations in the listed files. Retain the existing approval sentence structure until Task 7; only registry ownership changes here.

Representative expected values:

```text
ghcr.io/supergate-hub/kolla-container-images/keystone:2025.1-rocky-9-amd64
ghcr.io/supergate-hub/kolla-container-images/keystone:2025.1-rocky-9-arm64
ghcr.io/supergate-hub/kolla-container-images/keystone:2025.1-rocky-9
```

- [ ] **Step 5: Run focused and full GREEN checks**

Run:

```bash
python3 -m unittest tests/test_namespace_transition.py tests/test_plan_publish.py tests/test_lock_generation.py tests/test_publish_approval.py -v
python3 -m unittest discover -s tests -v
```

Expected: namespace tests and the existing suite pass with organization refs.

Do not commit.

---

### Task 2: Add the Shared Stream/Profile Resolver

**Files:**

- Create: `scripts/profile_resolver.py`
- Create: `tests/test_profile_resolution.py`

**Interfaces:**

```python
def load_matrix(path: Path = MATRIX_PATH) -> dict[str, Any]
def stream_ids(matrix: dict[str, Any]) -> list[str]
def find_stream(matrix: dict[str, Any], stream_id: str) -> dict[str, Any]
def load_profile(name: str, profiles_dir: Path = PROFILES_DIR) -> dict[str, Any]
def selector_matches(applies_to: dict[str, list[str]] | None, stream: dict[str, Any]) -> bool
def resolve_profile(profile: dict[str, Any], stream: dict[str, Any]) -> dict[str, Any]
def render_tag(matrix: dict[str, Any], stream: dict[str, Any], arch: str | None = None) -> str
```

- [ ] **Step 1: Write resolver tests before the module exists**

Create an in-memory two-stream schema-v3 fixture and assert:

```python
def test_find_stream_lists_accepted_ids_on_failure(self) -> None:
    with self.assertRaisesRegex(ValueError, "accepted streams: alpha, beta"):
        find_stream(self.matrix, "missing")

def test_selector_dimensions_are_anded(self) -> None:
    selector = {"releases": ["2026.1"], "distros": ["ubuntu"]}
    self.assertTrue(selector_matches(selector, self.ubuntu_2026))
    self.assertFalse(selector_matches(selector, self.rocky_2025))

def test_architecture_selector_fails_closed(self) -> None:
    with self.assertRaisesRegex(ValueError, "unsupported applies_to keys"):
        selector_matches({"architectures": ["arm64"]}, self.ubuntu_2026)

def test_unreviewed_stream_fails_closed(self) -> None:
    with self.assertRaisesRegex(ValueError, "has not reviewed stream"):
        resolve_profile(self.profile, {**self.rocky_2025, "id": "unreviewed"})
```

Also assert that image selectors, conditional variable objects, empty-group removal, original ordering, and parent lists resolve to plain JSON structures.

- [ ] **Step 2: Run resolver tests and verify RED**

Run:

```bash
python3 -m unittest tests/test_profile_resolution.py -v
```

Expected: import failure because `scripts/profile_resolver.py` is absent.

- [ ] **Step 3: Implement `scripts/profile_resolver.py`**

Use this complete resolver core:

```python
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
MATRIX_PATH = ROOT / "config" / "build-matrix.json"
PROFILES_DIR = ROOT / "config" / "profiles"
SELECTOR_FIELDS = {"streams": "id", "releases": "release", "distros": "distro"}


def load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as file_obj:
        return json.load(file_obj)


def load_matrix(path: Path = MATRIX_PATH) -> dict[str, Any]:
    return load_json(path)


def stream_ids(matrix: dict[str, Any]) -> list[str]:
    return [stream["id"] for stream in matrix["streams"]]


def find_stream(matrix: dict[str, Any], stream_id: str) -> dict[str, Any]:
    for stream in matrix["streams"]:
        if stream["id"] == stream_id:
            return stream
    accepted = ", ".join(stream_ids(matrix))
    raise ValueError(f"unsupported stream: {stream_id}; accepted streams: {accepted}")


def load_profile(name: str, profiles_dir: Path = PROFILES_DIR) -> dict[str, Any]:
    path = profiles_dir / f"{name}.json"
    if not path.exists():
        raise ValueError(f"profile does not exist: {path.relative_to(ROOT)}")
    profile = load_json(path)
    if profile.get("name") != name:
        raise ValueError(f"profile name mismatch in {path.relative_to(ROOT)}")
    return profile


def selector_matches(
    applies_to: dict[str, list[str]] | None,
    stream: dict[str, Any],
) -> bool:
    if applies_to is None:
        return True
    unknown = set(applies_to) - set(SELECTOR_FIELDS)
    if unknown:
        raise ValueError(f"unsupported applies_to keys: {sorted(unknown)}")
    if not applies_to:
        raise ValueError("applies_to must not be empty")
    return all(
        stream[SELECTOR_FIELDS[field]] in accepted
        for field, accepted in applies_to.items()
    )


def resolve_profile(profile: dict[str, Any], stream: dict[str, Any]) -> dict[str, Any]:
    if profile.get("schema_version") != 3:
        raise ValueError(f"profile {profile.get('name')!r} schema_version must be 3")
    if stream["id"] not in profile.get("reviewed_streams", []):
        raise ValueError(
            f"profile {profile.get('name')!r} has not reviewed stream {stream['id']!r}"
        )
    resolved_images: list[dict[str, Any]] = []
    for raw_image in profile["images"]:
        if not selector_matches(raw_image.get("applies_to"), stream):
            continue
        variables: list[str] = []
        for raw_variable in raw_image["kolla_ansible_variables"]:
            if isinstance(raw_variable, str):
                variables.append(raw_variable)
            elif selector_matches(raw_variable.get("applies_to"), stream):
                variables.append(raw_variable["name"])
        image = copy.deepcopy(raw_image)
        image.pop("applies_to", None)
        image["kolla_ansible_variables"] = variables
        resolved_images.append(image)
    resolved_names = {image["name"] for image in resolved_images}
    resolved_groups: list[dict[str, Any]] = []
    for raw_group in profile["build_groups"]:
        images = [name for name in raw_group["images"] if name in resolved_names]
        if images:
            group = copy.deepcopy(raw_group)
            group["images"] = images
            resolved_groups.append(group)
    resolved = copy.deepcopy(profile)
    resolved["images"] = resolved_images
    resolved["build_groups"] = resolved_groups
    resolved["resolved_stream"] = stream["id"]
    return resolved


def render_tag(
    matrix: dict[str, Any],
    stream: dict[str, Any],
    arch: str | None = None,
) -> str:
    template_name = "arch_tag_template" if arch else "deploy_tag_template"
    return matrix["tag_policy"][template_name].format(
        stream=stream["id"],
        release=stream["release"],
        distro=stream["distro"],
        base_tag=stream["base_tag"],
        tag_token=stream["tag_token"],
        arch=arch or "",
    )
```

- [ ] **Step 4: Run resolver and unchanged-suite GREEN checks**

Run:

```bash
python3 -m unittest tests/test_profile_resolution.py -v
python3 -m unittest discover -s tests -v
```

Expected: resolver tests pass and existing tests remain green because production scripts are not wired to the resolver yet.

Do not commit.

---

### Task 3: Activate the Seven-Stream Matrix and Schema-v3 Profiles

**Files:**

- Create: `tests/test_config_validation.py`
- Modify: `config/build-matrix.json`
- Modify: `config/profiles/core.json`
- Modify: `config/profiles/deployment.json`
- Modify: `scripts/validate-config.py`

**Interfaces:**

- Consumes: `profile_resolver.find_stream()`, `resolve_profile()`, and `stream_ids()`.
- Produces: valid matrix schema v2 and raw/resolved profile policy checks for exactly seven streams.

- [ ] **Step 1: Add failing matrix/profile policy tests**

Create `tests/test_config_validation.py` with direct JSON assertions plus subprocess validation. Use this exact policy table:

```python
EXPECTED_STREAMS = {
    "2025.1-rocky-9": ("2025.1", "20.4.0", "20.4.0", "rocky", "9", "9"),
    "2025.1-rocky-10": ("2025.1", "20.4.0", "20.4.0", "rocky", "10", "10"),
    "2025.1-ubuntu-noble": ("2025.1", "20.4.0", "20.4.0", "ubuntu", "24.04", "noble"),
    "2025.2-rocky-10": ("2025.2", "21.1.0", "21.1.0", "rocky", "10", "10"),
    "2025.2-ubuntu-noble": ("2025.2", "21.1.0", "21.1.0", "ubuntu", "24.04", "noble"),
    "2026.1-rocky-10": ("2026.1", "22.0.0", "22.0.0", "rocky", "10", "10"),
    "2026.1-ubuntu-noble": ("2026.1", "22.0.0", "22.0.0", "ubuntu", "24.04", "noble"),
}
```

Assert matrix schema 2, organization owner, matching Kolla/Kolla-Ansible pins, separate `base_tag`/`tag_token` values, `publish_enabled is True`, exact architectures, profile schema 3, exact reviewed-stream sets, and successful `scripts/validate-config.py` execution. Validate that rendering each deploy tag exactly reproduces its stream ID.

- [ ] **Step 2: Run configuration tests and verify RED**

Run:

```bash
python3 -m unittest tests/test_config_validation.py -v
```

Expected: FAIL because the matrix is schema 1 with one top-level release and profiles are schema 2.

- [ ] **Step 3: Replace the matrix with the exact stream model**

Set `config/build-matrix.json` to:

```json
{
  "schema_version": 2,
  "owner": "supergate-hub",
  "repository": "kolla-container-images",
  "registry": "ghcr.io",
  "profiles": ["core", "deployment"],
  "streams": [
    {"id": "2025.1-rocky-9", "release": "2025.1", "kolla_version": "20.4.0", "kolla_ansible_version": "20.4.0", "distro": "rocky", "base_tag": "9", "tag_token": "9", "publish_enabled": true},
    {"id": "2025.1-rocky-10", "release": "2025.1", "kolla_version": "20.4.0", "kolla_ansible_version": "20.4.0", "distro": "rocky", "base_tag": "10", "tag_token": "10", "publish_enabled": true},
    {"id": "2025.1-ubuntu-noble", "release": "2025.1", "kolla_version": "20.4.0", "kolla_ansible_version": "20.4.0", "distro": "ubuntu", "base_tag": "24.04", "tag_token": "noble", "publish_enabled": true},
    {"id": "2025.2-rocky-10", "release": "2025.2", "kolla_version": "21.1.0", "kolla_ansible_version": "21.1.0", "distro": "rocky", "base_tag": "10", "tag_token": "10", "publish_enabled": true},
    {"id": "2025.2-ubuntu-noble", "release": "2025.2", "kolla_version": "21.1.0", "kolla_ansible_version": "21.1.0", "distro": "ubuntu", "base_tag": "24.04", "tag_token": "noble", "publish_enabled": true},
    {"id": "2026.1-rocky-10", "release": "2026.1", "kolla_version": "22.0.0", "kolla_ansible_version": "22.0.0", "distro": "rocky", "base_tag": "10", "tag_token": "10", "publish_enabled": true},
    {"id": "2026.1-ubuntu-noble", "release": "2026.1", "kolla_version": "22.0.0", "kolla_ansible_version": "22.0.0", "distro": "ubuntu", "base_tag": "24.04", "tag_token": "noble", "publish_enabled": true}
  ],
  "architectures": ["amd64", "arm64"],
  "tag_policy": {
    "deploy_tag_template": "{release}-{distro}-{tag_token}",
    "arch_tag_template": "{release}-{distro}-{tag_token}-{arch}"
  }
}
```

- [ ] **Step 4: Upgrade both profile headers and Neutron mappings**

Set `schema_version` to 3 and add the seven IDs under `reviewed_streams` in both profiles. In every profile containing `neutron-server`, use:

```json
[
  "neutron_server_image_full",
  {"name": "neutron_rpc_server_image_full", "applies_to": {"releases": ["2025.2", "2026.1"]}},
  {"name": "neutron_periodic_worker_image_full", "applies_to": {"releases": ["2025.2", "2026.1"]}},
  {"name": "neutron_ovn_maintenance_worker_image_full", "applies_to": {"releases": ["2025.2", "2026.1"]}}
]
```

- [ ] **Step 5: Rewrite configuration validation around raw and resolved invariants**

Import the resolver. `validate_matrix()` checks schema 2, exact registry identity, unique/exact stream IDs, pins/base tags, boolean publish flags, exact architectures, and exact tag-template fields. Add these concrete interfaces:

```text
validate_selector(selector: Any, matrix: dict[str, Any], context: str, errors: list[str]) -> None
validate_profile(matrix: dict[str, Any], profile_name: str, profile: dict[str, Any], errors: list[str]) -> None
```

`validate_selector()` permits only `streams`, `releases`, and `distros`, requires non-empty string lists, and checks matrix membership. `validate_profile()` preserves current raw name/variable/group/parent/coverage checks, adds schema/review/selector validation, resolves all seven streams, and repeats variable uniqueness plus exact group coverage on each plain resolved profile. Append resolver `ValueError` messages to `errors`.

- [ ] **Step 6: Run JSON, resolver, and configuration checks GREEN**

Run:

```bash
while IFS= read -r file; do python3 -m json.tool "$file" >/dev/null; done < <(rg --files -g '*.json')
python3 -m unittest tests/test_profile_resolution.py tests/test_config_validation.py -v
python3 scripts/validate-config.py
```

Expected: all JSON parses, tests pass, and validation prints `Configuration validation passed.`

The planner, summary, and lock scripts are temporarily incompatible with matrix v2 and are migrated before the full-suite checkpoint. Do not commit.

---

### Task 4: Add the Exact Mixed-Backend Deployment Closure

**Files:**

- Modify: `config/profiles/deployment.json`
- Modify: `tests/test_deployment_profile.py`
- Modify: `tests/test_config_validation.py`

**Interfaces:**

- Consumes: schema-v3 selectors and `resolve_profile(profile, stream)`.
- Produces: exact 63/64/65/66 leaf closures with complete build-group and variable coverage.

- [ ] **Step 1: Replace raw-52 tests with per-stream resolved tests**

Use:

```python
EXPECTED_COUNTS = {
    "2025.1-rocky-9": 63,
    "2025.1-rocky-10": 63,
    "2025.1-ubuntu-noble": 64,
    "2025.2-rocky-10": 63,
    "2025.2-ubuntu-noble": 64,
    "2026.1-rocky-10": 65,
    "2026.1-ubuntu-noble": 66,
}

COMMON_ADDITIONS = {
    "cinder-api", "cinder-scheduler", "cinder-volume", "cinder-backup",
    "manila-api", "manila-scheduler", "manila-share", "manila-data",
    "iscsid", "valkey-server", "valkey-sentinel",
}

NEW_2026_EXPORTERS = {
    "prometheus-openstack-network-exporter",
    "prometheus-valkey-exporter",
}
```

For every stream assert exact count, common additions, unique group coverage, `tgtd` iff Ubuntu, new exporters iff 2026.1, no `etcd`/`multipathd`/Redis leaves, and three Neutron aliases iff release 2025.2/2026.1.

- [ ] **Step 2: Run deployment closure tests and capture RED**

Run:

```bash
python3 -m unittest tests/test_deployment_profile.py -v 2>&1 | tee .context/red-deployment-closure.txt
```

Expected: FAIL because deployment still resolves to 52 leaves.

- [ ] **Step 3: Add exact build groups**

```json
{"name": "coordination", "parent": "valkey-base", "parents": ["base", "valkey-base"], "images": ["valkey-server", "valkey-sentinel"]},
{"name": "storage-runtime", "parent": "base", "parents": ["base"], "images": ["iscsid", "tgtd"]},
{"name": "cinder", "parent": "cinder-base", "parents": ["base", "openstack-base", "cinder-base"], "images": ["cinder-api", "cinder-backup", "cinder-scheduler", "cinder-volume"]},
{"name": "manila", "parent": "manila-base", "parents": ["base", "openstack-base", "manila-base"], "images": ["manila-api", "manila-data", "manila-scheduler", "manila-share"]}
```

Append both 2026.1 exporter names to the existing monitoring group.

- [ ] **Step 4: Add exact image mappings**

Common mappings:

```json
{"name": "cinder-api", "kolla_ansible_variables": ["cinder_api_image_full"]},
{"name": "cinder-backup", "kolla_ansible_variables": ["cinder_backup_image_full"]},
{"name": "cinder-scheduler", "kolla_ansible_variables": ["cinder_scheduler_image_full"]},
{"name": "cinder-volume", "kolla_ansible_variables": ["cinder_volume_image_full"]},
{"name": "iscsid", "kolla_ansible_variables": ["iscsid_image_full"]},
{"name": "manila-api", "kolla_ansible_variables": ["manila_api_image_full"]},
{"name": "manila-data", "kolla_ansible_variables": ["manila_data_image_full"]},
{"name": "manila-scheduler", "kolla_ansible_variables": ["manila_scheduler_image_full"]},
{"name": "manila-share", "kolla_ansible_variables": ["manila_share_image_full"]},
{"name": "valkey-server", "kolla_ansible_variables": ["valkey_image_full"]},
{"name": "valkey-sentinel", "kolla_ansible_variables": ["valkey_sentinel_image_full"]}
```

Conditional mappings:

```json
{"name": "tgtd", "kolla_ansible_variables": ["tgtd_image_full"], "applies_to": {"distros": ["ubuntu"]}},
{"name": "prometheus-openstack-network-exporter", "kolla_ansible_variables": ["prometheus_openstack_network_exporter_image_full"], "applies_to": {"releases": ["2026.1"]}},
{"name": "prometheus-valkey-exporter", "kolla_ansible_variables": ["prometheus_valkey_exporter_image_full"], "applies_to": {"releases": ["2026.1"]}}
```

- [ ] **Step 5: Enforce closure policy and run GREEN**

For each resolved deployment stream, validate the exact count, conditional leaves, required Cinder/Manila/Octavia/Valkey/logging/Grafana/Prometheus sets, and excluded leaves. Run:

```bash
python3 -m unittest tests/test_deployment_profile.py tests/test_config_validation.py -v
python3 scripts/validate-config.py
```

Expected: all seven closures pass. Do not commit.

---

### Task 5: Migrate the Dry-Run Planner to Stream IDs and Native Build Units

**Files:**

- Modify: `scripts/plan-publish.py`
- Modify: `tests/test_plan_publish.py`
- Modify: `tests/test_repository_boundary.py`

**Interfaces:**

```python
def build_plan(
    matrix: dict[str, Any],
    profile: dict[str, Any],
    stream: dict[str, Any],
    image_filter: str | None = None,
) -> dict[str, Any]
```

- [ ] **Step 1: Convert tests to `--stream` and add seven-stream expectations**

Use final commands such as:

```bash
python3 scripts/plan-publish.py --stream 2025.1-rocky-9 --profile core --dry-run
```

Test every stream's Kolla/base values, organization refs, deploy/arch tags, platforms, resolved deployment count, invalid-stream list, Ubuntu `24.04`/`noble` split, and absence of candidate-lock paths from core or partial plans. Replace parent/group job assertions with exactly two `build.architectures` entries and one Kolla command per architecture without `--skip-existing`.

- [ ] **Step 2: Run planner tests and capture RED**

```bash
python3 -m unittest tests/test_plan_publish.py tests/test_repository_boundary.py -v 2>&1 | tee .context/red-stream-planner.txt
```

Expected: FAIL because `--stream` is unrecognized.

- [ ] **Step 3: Load stream and resolved profile**

`parse_args()` accepts `--stream`, `--profile`, optional `--image`, and required `--dry-run`. `main()` uses:

```python
matrix = load_matrix()
stream = find_stream(matrix, args.stream)
profile = resolve_profile(load_profile(args.profile), stream)
plan = build_plan(matrix, profile, stream, args.image)
```

Use `render_tag()` for every tag; never derive Ubuntu deploy tags from `base_tag`.

- [ ] **Step 4: Emit one Kolla command per architecture**

Each command has the exact stream base/pin inputs, organization namespace, architecture tag, bounded threads, one architecture summary/log path, `--push`, and one anchored regex per selected leaf. Do not use `--skip-existing`. Kolla auto-builds parents.

Each `build.architectures` entry contains `arch`, `kolla_base_arch`, `platform`, native runner label, parent names/refs, leaf refs, and a per-leaf smoke policy. The plan cannot know a child digest before publication, so it identifies the architecture tag as the digest source and requires all smoke commands to consume the immutable ref recorded after push:

```python
{
    "arch_ref": arch_ref,
    "smoke": {
        "ref_source": "recorded_child_digest",
        "platform": platform,
        "inspect_platform": True,
        "entrypoint": "/bin/true",
    },
}
```

- [ ] **Step 5: Emit exact scope and artifact fields**

```python
{
    "stream": stream["id"],
    "release": stream["release"],
    "distro": stream["distro"],
    "distro_version": stream["base_tag"],
    "kolla_version": stream["kolla_version"],
    "kolla_ansible_version": stream["kolla_ansible_version"],
    "profile": profile["name"],
    "image_filter": image_filter,
    "scope": {
        "profile": profile["name"],
        "image": image_filter or "all",
        "image_count": len(selected_images),
    },
    "publish_summary_file": publish_summary_file(stream["id"]),
    "kolla_ansible_lock_file": (
        kolla_ansible_lock_file(stream["id"])
        if profile["name"] == "deployment" and image_filter is None
        else None
    ),
}
```

Parents are listed in architecture evidence only, never in deployable images, manifest commands, summary leaf coverage, or locks.

- [ ] **Step 6: Run planner GREEN across all streams**

```bash
python3 -m unittest tests/test_plan_publish.py tests/test_repository_boundary.py -v
for stream in 2025.1-rocky-9 2025.1-rocky-10 2025.1-ubuntu-noble 2025.2-rocky-10 2025.2-ubuntu-noble 2026.1-rocky-10 2026.1-ubuntu-noble; do
  python3 scripts/plan-publish.py --stream "$stream" --profile core --dry-run >/dev/null
  python3 scripts/plan-publish.py --stream "$stream" --profile deployment --dry-run >/dev/null
done
python3 scripts/plan-publish.py --stream 2025.1-rocky-9 --profile core --image keystone --dry-run >/dev/null
```

Expected: focused tests and all 15 read-only plans pass. Do not commit.

---

### Task 6: Make Publish Summaries and the Candidate Lock Stream-Aware

**Files:**

- Modify: `scripts/validate-publish-summary.py`
- Modify: `scripts/generate-lock.py`
- Modify: `tests/test_publish_summary_validation.py`
- Modify: `tests/test_lock_generation.py`

**Interfaces:**

```text
validate_publish_summary(matrix, profile, stream, summary, allow_partial, image_filter) -> list[str]
render_lock(matrix, profile, stream, summary) -> str
```

- [ ] **Step 1: Convert fixtures and CLI tests to stream IDs**

Build summary fixtures from `find_stream()` plus `resolve_profile()` rather than raw profile JSON. Every fixture includes the exact `stream`, `release`, `distro`, `distro_version`, registry identity, selected leaves, architecture refs/platforms, child digests, deploy ref, and multi-arch manifest digest.

Cover these cases before implementation:

- a full deployment summary passes for Rocky 9, Ubuntu Noble, and 2026.1;
- the expected resolved count is enforced for all seven streams;
- a missing or extra conditional leaf fails;
- a wrong owner, stream, deploy ref, arch ref, platform, child digest, or manifest digest fails;
- a partial core/Keystone summary passes only with `--allow-partial --image keystone`;
- candidate-lock generation rejects `core`, partial deployment, missing images, extra images, and scope mismatches;
- a complete deployment summary produces all resolved aliases and no aliases that do not apply to that stream.

- [ ] **Step 2: Run summary/lock tests and capture RED**

```bash
python3 -m unittest tests/test_publish_summary_validation.py tests/test_lock_generation.py -v 2>&1 | tee .context/red-stream-artifacts.txt
```

Expected: FAIL because both scripts still require release/distro arguments and load raw profiles.

- [ ] **Step 3: Rewrite summary validation around one selected stream**

Replace `--release`, `--distro`, and `--distro-version` with required `--stream`. Resolve the requested profile against that stream and validate the summary against this exact scope:

```python
{
    "stream": stream["id"],
    "release": stream["release"],
    "distro": stream["distro"],
    "distro_version": stream["base_tag"],
    "profile": profile["name"],
    "registry": matrix["registry"],
    "owner": matrix["owner"],
    "repository": matrix["repository"],
}
```

For every selected leaf, require exactly `amd64` and `arm64`; validate `linux/<arch>`, the stream-specific architecture ref, a valid architecture digest, the architecture-neutral deploy ref, and the manifest digest. Require exact resolved leaf coverage and variable mappings. Parent artifacts are not accepted as summary images.

The summary carries the same frozen scope as the plan:

```python
"scope": {
    "profile": profile["name"],
    "image": image_filter or "all",
    "image_count": len(expected_images),
}
```

- [ ] **Step 4: Restrict lock generation to a complete deployment scope**

The final CLI is:

```bash
python3 scripts/generate-lock.py \
  --publish-summary artifacts/publish-summary-2025.1-rocky-9.json \
  --stream 2025.1-rocky-9 \
  --profile deployment \
  --candidate-id 123456789-1 \
  --output artifacts/kolla-ansible-image-lock-2025.1-rocky-9.yml
```

Reject any profile other than `deployment`, any summary whose scope is not exact `deployment/all`, a scope count mismatch, and any non-exact resolved coverage. Write each architecture-neutral tag-only Kolla-Ansible variable at the root, with its deploy reference, multi-architecture manifest digest, and immutable reference in the reserved metadata mapping:

```yaml
_kolla_candidate_lock:
  images:
    "nova-compute":
      deploy_ref: "ghcr.io/supergate-hub/kolla-container-images/nova-compute:2025.1-rocky-9-candidate-123456789-1"
      manifest_digest: "sha256:<multi-arch-manifest-digest>"
      immutable_ref: "ghcr.io/supergate-hub/kolla-container-images/nova-compute@sha256:<multi-arch-manifest-digest>"
nova_compute_image_full: "ghcr.io/supergate-hub/kolla-container-images/nova-compute:2025.1-rocky-9-candidate-123456789-1"
```

The generated file is a generic candidate lock only. Before deployment, `openstack-infra-ops` verifies each `deploy_ref` against `manifest_digest` and the bytes returned by `immutable_ref`. The lock contains no environment name, promotion state, pointer, host inventory, or deployment action.

- [ ] **Step 5: Run focused GREEN checks**

```bash
python3 -m unittest tests/test_publish_summary_validation.py tests/test_lock_generation.py -v
python3 scripts/plan-publish.py --stream 2025.1-rocky-9 --profile deployment --dry-run >/dev/null
python3 scripts/plan-publish.py --stream 2025.1-ubuntu-noble --profile deployment --dry-run >/dev/null
```

Expected: exact stream coverage and candidate-lock boundary tests pass. Do not commit.

---

### Task 7: Bind Approval to the Frozen Plan and Refactor the Publish Workflow

**Files:**

- Create: `scripts/publish_approval.py`
- Modify: `scripts/plan-publish.py`
- Modify: `scripts/validate-publish-approval.py`
- Modify: `.github/workflows/publish.yml`
- Modify: `.github/workflows/validate.yml`
- Modify: `tests/test_publish_approval.py`
- Modify: `tests/test_publish_workflow.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class ApprovalRequirement:
    variable: str
    phrase: str


def approval_requirement(
    registry_path: str,
    stream: str,
    profile: str,
    image: str,
    image_count: int,
) -> ApprovalRequirement | None
```

```text
python3 scripts/validate-publish-approval.py --publish-plan <plan.json>
PUBLISH ghcr.io/supergate-hub/kolla-container-images <stream> <profile>/<image> (<count> image|images, amd64/arm64)
```

- [ ] **Step 1: Add failing approval tests for every stream and scope**

Generate frozen plan fixtures through `scripts/plan-publish.py`, then test all 21 positive combinations: seven streams times `core/keystone`, `core/all`, and `deployment/all`. The exact environment-variable mapping is:

```python
{
    ("core", "keystone"): "ALLOW_GHCR_PUBLISH",
    ("core", "all"): "ALLOW_GHCR_FULL_CORE_PUBLISH",
    ("deployment", "all"): "ALLOW_GHCR_DEPLOYMENT_PUBLISH",
}
```

Also reject a false/missing variable, wrong phrase, stale count, altered namespace, altered stream, altered image selection, unsupported partial deployment, and a plan without exactly `amd64`/`arm64`. Assert singular `1 image` for Keystone and plural `N images` for full scopes.

- [ ] **Step 2: Add failing workflow-structure tests**

Historical requirement (superseded): require both `workflow_dispatch` and
`workflow_call`, plus the remaining workflow controls described here. The
current trigger contract uses only a separate `workflow_dispatch` run; all
other scope and evidence requirements in this step remain historical context.

Run:

```bash
python3 -m unittest tests/test_publish_approval.py tests/test_publish_workflow.py -v 2>&1 | tee .context/red-frozen-plan-workflow.txt
```

Expected: FAIL because approval still accepts independent scope arguments and the workflow uses the old release/distro matrix.

- [ ] **Step 3: Make the validator recompute approval from the plan**

Implement `publish_approval.py` with only the three allowed scope mappings. It formats `image` when `image_count == 1`, otherwise `images`, and returns `None` for every other scope. Update the planner to include:

```python
"approval": {
    "allowed": requirement is not None,
    "required_variable": requirement.variable if requirement else None,
    "phrase": requirement.phrase if requirement else None,
}
```

Load `--publish-plan`, reload the repository matrix/profile, re-find the stream, re-resolve the profile, and recompute the selected leaf set, count, namespace, platforms, and allowed scope through the same helper. Do not trust approval text or count stored in the plan. Compare the recomputed values with the frozen plan; only then select the scope variable and compare `APPROVAL` with the exact phrase. This also guarantees that every dry-run plan shows either the exact usable phrase or an explicit `allowed: false` for dry-run-only partial scopes.

Example phrases:

```text
PUBLISH ghcr.io/supergate-hub/kolla-container-images 2025.1-rocky-9 core/keystone (1 image, amd64/arm64)
PUBLISH ghcr.io/supergate-hub/kolla-container-images 2026.1-ubuntu-noble deployment/all (66 images, amd64/arm64)
```

- [ ] **Step 4: Replace workflow inputs and establish the read-only default**

Both triggers accept `stream`, `profile`, `image`, `dry_run`, and `approval`. `stream` is a string for both triggers and is strictly validated by the planner, which reports all seven accepted IDs. Normalize input `image=all` by omitting `--image`; every other value is passed as the planner's `--image` argument. Planning writes and uploads one immutable JSON artifact and performs no registry login.

Serialize writers with `concurrency.group: kolla-publish-${{ inputs.stream }}` and `cancel-in-progress: false`. Bound cross-stream builds to two to four through native runner-pool capacity.

Keep workflow-level permissions at `contents: read`. The plan job runs `python3 scripts/validate-config.py` before `plan-publish.py`, writes exactly `artifacts/plan/publish-plan.json`, uploads artifact `publish-plan`, and has no GHCR login or `packages: write`. Use these exact mutation predicates:

```yaml
authorize-publish:
  if: ${{ !inputs.dry_run }}
build-native:
  if: ${{ !inputs.dry_run }}
finalize-publish:
  if: ${{ !inputs.dry_run }}
```

The non-dry authorization job:

- downloads that exact plan artifact;
- has `environment: ghcr-publish` and no `packages: write` permission;
- exposes only the three repository variables plus input approval to the validator;
- completes before either build job starts.

- [ ] **Step 5: Build each architecture natively from the frozen plan**

Use one `build-native` matrix job with exactly two native executions:

```yaml
strategy:
  max-parallel: 2
  matrix:
    include:
      - arch: amd64
        runner_arch: x64
        runner_machine: x86_64
      - arch: arm64
        runner_arch: ARM64
        runner_machine: aarch64
runs-on: [self-hosted, linux, "${{ matrix.runner_arch }}", kolla-build]
```

Each execution checks out the same commit, downloads artifact `publish-plan`, and re-runs approval validation before registry login. Verify `platform.machine()` equals `matrix.runner_machine`. Resolve Docker's root with `docker info --format '{{.DockerRootDir}}'`, read that filesystem's available 1 KiB blocks with `df -Pk`, and fail unless at least `150 * 1024 * 1024` blocks are free before login; document 300 GB as the operational target.

Use `actions/setup-python@v6` with pip caching keyed by `config/build-matrix.json`, create `.venv`, extract `plan["kolla_version"]` into `KOLLA_VERSION`, verify it equals the selected stream's matrix pin, and install exactly `"kolla==$KOLLA_VERSION"` with `"docker==7.1.0"`. Import the Docker SDK, require version 7.1.0, and exercise `kolla-build --version` before registry login. Remove the workflow-level hard-coded Kolla version; do not create a builder image or follow a moving branch. Execute the plan's single Kolla command for that architecture.

For every pushed architecture tag, run `docker buildx imagetools inspect <arch-ref> --format '{{json .Manifest}}'` and require one SHA256 descriptor for the expected repository/platform. Form `<repository>@<descriptor-digest>`, pull that immutable ref with `--platform`, inspect its OS/architecture, and run each leaf with entrypoint `/bin/true`. Parents get the same immutable digest inspection but no deploy smoke.

Write and upload `artifacts/arch/native-${arch}.json` with this exact schema:

```json
{
  "schema_version": 1,
  "stream": "2025.1-rocky-9",
  "arch": "amd64",
  "platform": "linux/amd64",
  "runner_machine": "x86_64",
  "kolla_version": "20.4.0",
  "parents": [
    {"image": "base", "arch_ref": "tagged ref", "digest": "sha256:...", "immutable_ref": "digest ref"}
  ],
  "images": [
    {
      "image": "keystone",
      "arch_ref": "tagged ref",
      "digest": "sha256:...",
      "immutable_ref": "digest ref",
      "smoke": {"platform": "linux/amd64", "entrypoint": "/bin/true", "passed": true}
    }
  ]
}
```

Validate exact parent and leaf sets against the frozen plan before upload. Upload the files under exact artifact names `native-amd64` and `native-arm64`; do not use a wildcard merge. Do not install or invoke QEMU.

- [ ] **Step 6: Create and verify architecture-neutral manifests**

The finalize job is the only other job with `packages: write`. It checks out the same commit, downloads the frozen plan and both exact architecture evidence artifacts, and re-runs approval validation before registry login. It rejects a stream/scope/pin mismatch and missing/extra/duplicate parents or leaves, then creates each deploy tag from immutable child references:

```text
ghcr.io/supergate-hub/kolla-container-images/<image>@sha256:<amd64-child-digest>
ghcr.io/supergate-hub/kolla-container-images/<image>@sha256:<arm64-child-digest>
```

Save `docker buildx imagetools inspect --raw <deploy-ref>` and parse its manifest descriptors. Accept exactly the standard OCI image-index media type `application/vnd.oci.image.index.v1+json` or Docker manifest-list media type `application/vnd.docker.distribution.manifest.list.v2+json`; require the metadata descriptor and raw media types to match. Require exactly two descriptors, exactly `linux/amd64` and `linux/arm64`, and the recorded child digest on its corresponding platform; reject annotations or duplicate platform entries that obscure this mapping. Obtain and validate the SHA256 multi-architecture manifest digest from the manifest-create metadata. Emit exactly `artifacts/publish-summary-<stream>.json`, validate it, and generate exactly `artifacts/kolla-ansible-image-lock-<stream>.yml` only when the frozen plan is `deployment/all`. Parent refs never become manifest, summary, or lock entries.

- [ ] **Step 7: Update validation CI and run workflow GREEN checks**

`validate.yml` parses every JSON file, runs configuration validation, exercises `core/deployment` and Keystone dry-run plans, and runs the complete unit suite. Then run locally:

```bash
python3 -m unittest tests/test_publish_approval.py tests/test_publish_workflow.py -v
actionlint .github/workflows/*.yml
```

Expected: approval and workflow contract tests pass and `actionlint` reports no findings. Do not dispatch the workflow and do not commit.

---

### Task 8: Document Consumption, Manual Setup, and Repository Boundaries

**Files:**

- Modify: `README.md`
- Modify: `docs/publish.md`
- Modify: `docs/build-readiness.md`
- Modify: `tests/test_repository_boundary.py`

- [ ] **Step 1: Add failing documentation/boundary assertions**

Require documentation for all seven stream IDs, organization namespace, Ubuntu `24.04` versus `noble`, architecture-neutral multi-arch consumption, `openstack_tag_suffix: ""`, native ARM64 evidence, three scope variables, protected environment, runner labels/capacity, Cinder/Manila/Octavia/observability coverage, and the generic candidate-lock boundary. Forbid environment lock/tag/pointer generation and deployment commands from source/workflow paths. Assert that docs keep registry credentials, OpenStack credentials, Ceph keys, private CAs, and site-specific configuration outside images and generated locks.

```bash
python3 -m unittest tests/test_repository_boundary.py -v 2>&1 | tee .context/red-documentation-boundary.txt
```

Expected: FAIL until the operator contract is updated.

- [ ] **Step 2: Update the repository overview and stream policy**

Explain that `2025.1-rocky-9` is the standing deployment baseline, while the other six streams remain build/manifest/digest/native-smoke/lock compatibility streams until promotion outside this repository. All seven are publish-capable behind the same gates; when a future stream becomes primary, external operations validate it on shared Stg before Prod. Record the actual topology: Dev may clone two or three per-user labs on `bb00`, Stg is one shared cluster on `bb01`/`bb02`, and Prod is one IDC cluster. Current Dev/Stg deployment is AMD64; ARM64 is built and smoked on native CI, and a future ARM64 physical node joins the same mixed-architecture cluster rather than creating another logical cluster. State that OpenStack node OS and Kolla container base must match; a Rocky 9 lab VM on an Ubuntu physical host satisfies this. Keep management/workload Kubernetes explicitly outside the OpenStack cluster count and outside this repository's responsibility.

Document the environment backend contract: Dev uses per-lab Cinder LVM/LIO plus Manila Generic with DHSS/NFS; Stg and Prod use external Ceph RBD plus CephFS NFS through cephadm-managed NFS-Ganesha; disposable compatibility smoke uses matching-OS LVM with LIO on Rocky or TGT on Ubuntu and Generic/NFS. Ceph provisioning, pools, identities, config, keys, and NFS-Ganesha HA remain external. Octavia Amphora and Manila Generic share-server guest images live in Glance, require architecture-compatible variants and scheduling, and are not Kolla container artifacts.

State that standing Dev/Stg/Prod and compatibility smoke enable Prometheus, Grafana, Fluentd, OpenSearch, and OpenSearch Dashboards. Separate native image evidence (`stream × architecture × leaf`) from matching-OS Kolla-Ansible deployment-smoke evidence (`stream × architecture`) owned by `openstack-infra-ops` or a dedicated harness.

- [ ] **Step 3: Document Kolla-Ansible multi-arch consumption**

Show that operators use the generated lock as an extra-vars file alongside `globals.yml`; they do not choose `amd64` or `arm64` tags manually:

```yaml
# globals.yml
docker_registry: ghcr.io
docker_namespace: supergate-hub/kolla-container-images
docker_registry_insecure: "no"
openstack_release: "2025.1"
kolla_base_distro: rocky
kolla_base_distro_version: "9"
openstack_tag_suffix: ""
```

```yaml
# generated candidate lock supplied as an extra-vars file
_kolla_candidate_lock:
  images:
    "nova-compute":
      deploy_ref: "ghcr.io/supergate-hub/kolla-container-images/nova-compute:2025.1-rocky-9-candidate-123456789-1"
      manifest_digest: "sha256:<multi-arch-manifest-digest>"
      immutable_ref: "ghcr.io/supergate-hub/kolla-container-images/nova-compute@sha256:<multi-arch-manifest-digest>"
nova_compute_image_full: "ghcr.io/supergate-hub/kolla-container-images/nova-compute:2025.1-rocky-9-candidate-123456789-1"
```

Clarify that, before deployment, `openstack-infra-ops` verifies each `deploy_ref` against `manifest_digest` and the bytes returned by `immutable_ref`, then passes the tag-only root variables to Kolla-Ansible. Docker or Podman selects the matching child from the multi-architecture manifest automatically on homogeneous and mixed-architecture clusters. The lock is reviewed/copied by `openstack-infra-ops` or a dedicated external deployment/promotion system; this repository neither stores environment locks nor deploys them. Registry/OpenStack credentials, Ceph keys, private CAs, and site configuration remain in the external secret/configuration domains and are never embedded in an image or candidate lock.

- [ ] **Step 4: Document manual GitHub/GHCR setup without performing it**

List the operator actions needed before the first real publish:

1. create/verify native AMD64 and ARM64 self-hosted runner groups with the documented labels, capacity, Docker/Buildx, and network access;
2. create protected GitHub environment `ghcr-publish` with required reviewers and restrict deployment branches/tags;
3. define repository variables `ALLOW_GHCR_PUBLISH`, `ALLOW_GHCR_FULL_CORE_PUBLISH`, and `ALLOW_GHCR_DEPLOYMENT_PUBLISH` as `true` only during an approved window;
4. grant the workflow's `GITHUB_TOKEN` package write access and verify organization Actions/package policy permits GHCR publication;
   the former `workflow_call` permission ceiling is superseded by a separate
   CI `workflow_dispatch` credential with repository `Actions: write` and no
   package-write permission;
5. after first publication, set package visibility and repository linkage/permissions for the organization packages as required;
6. if packages remain private, provision a read-only `read:packages` service account for Kolla hosts and keep its credential outside this repository;
7. define retention, vulnerability-scanning, and package-cleanup policy;
8. use the exact count-bearing approval phrase from the dry-run plan and leave `dry_run: true` until all checks and reviewers are ready.

State explicitly that these settings were not changed during implementation.

- [ ] **Step 5: Run documentation/boundary GREEN checks**

```bash
python3 -m unittest tests/test_repository_boundary.py -v
python3 scripts/validate-config.py
```

Expected: boundary and configuration checks pass. Do not commit.

---

### Task 9: Run the Required Final Verification and Create One Local Commit

**Files:**

- Verify: all changed source, configuration, workflow, test, and documentation files
- Record evidence under: `.context/` (gitignored; do not commit)

- [ ] **Step 1: Run every JSON syntax check and configuration validation**

```bash
while IFS= read -r file; do
  python3 -m json.tool "$file" >/dev/null || exit 1
done < <(rg --files -g '*.json')
python3 scripts/validate-config.py
```

- [ ] **Step 2: Run required representative dry-run plans**

```bash
python3 scripts/plan-publish.py --stream 2025.1-rocky-9 --profile core --dry-run >.context/core-plan.json
python3 scripts/plan-publish.py --stream 2025.1-rocky-9 --profile deployment --dry-run >.context/deployment-plan.json
python3 scripts/plan-publish.py --stream 2025.1-rocky-9 --profile core --image keystone --dry-run >.context/keystone-plan.json
python3 scripts/plan-publish.py --stream 2025.1-ubuntu-noble --profile deployment --dry-run >.context/ubuntu-deployment-plan.json
```

Inspect representative refs, scope counts, parent evidence, two native architecture units, summary path, and candidate-lock path. Confirm core and Keystone plans have no lock path.

- [ ] **Step 3: Run the complete test and workflow checks**

```bash
python3 -m unittest discover -s tests -v 2>&1 | tee .context/final-tests.txt
actionlint .github/workflows/*.yml 2>&1 | tee .context/actionlint.txt
```

Record the final `Ran N tests` count from the test output.

- [ ] **Step 4: Prove namespace cleanup and diff hygiene**

Construct the previous owner name in the shell so the retired literal does not remain in this plan:

```bash
old_prefix='supergate-'
old_owner="${old_prefix}jhbyun"
rg -n --hidden \
  --glob '!.git/**' \
  --glob '!.context/**' \
  --glob '!**/__pycache__/**' \
  "$old_owner" .
git diff --check
git status --short --branch
git diff --stat origin/main...
git diff origin/main... -- config scripts tests .github README.md docs
```

Expected: the namespace search has no output, `git diff --check` succeeds, the current branch name is unchanged, and only in-scope files differ.

- [ ] **Step 5: Create exactly one local commit**

After all checks are green:

```bash
git add config scripts tests .github README.md docs
git diff --cached --check
git commit -m "feat: add multi-stream organization GHCR pipeline"
git rev-list --count origin/main..HEAD
git status --short --branch
```

Expected: commit count is exactly `1`, the workspace is clean, and the branch is still `jaehanbyun/ghcr-org-namespace-dry-run`.

Do not push, create a PR, dispatch a workflow, change GitHub variables/environments, or publish an image.

- [ ] **Step 6: Deliver the completion report**

Report all of the following from the checked-in state:

- changed files grouped by configuration, scripts/workflows, tests, and docs, with the core behavior change for each group;
- captured namespace RED, stream/closure/workflow RED, focused GREEN, final `Ran N tests`, JSON/config, dry-run, `actionlint`, namespace-search, and `git diff --check` evidence;
- representative organization refs for AMD64, ARM64, the architecture-neutral multi-architecture manifest, and a digest-bound candidate-lock entry;
- manual GitHub environment/variable, native-runner, GHCR package/access/visibility, and private-pull setup still required before publication;
- confirmation that generic candidate lock is the terminal responsibility and that environment locks, promotion, pointers, deployment, Ceph/site secrets, and guest-image lifecycle remain external;
- remaining risks or confirmations, especially native ARM64 service/deployment support, Valkey's older-release ARM evidence, runner capacity, per-leaf GHCR package bulk policy, first-package permissions/visibility, dedicated matching-OS deployment-smoke evidence, guest-appliance image pipelines, and reviewed updates for pinned Kolla versions.

Include the local commit hash and state explicitly that no push, PR, dispatch, variable change, or GHCR publish occurred.
