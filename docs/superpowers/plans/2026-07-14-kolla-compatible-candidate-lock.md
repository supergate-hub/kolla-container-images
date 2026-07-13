# Kolla-Compatible Candidate Lock Implementation Plan

> **Supersession note (2026-07-14):** Current execution follows
> [Kolla Publish Hardening Design](../specs/2026-07-14-publish-hardening-design.md).
> Candidate-qualified refs now feed locks, while the stable stream tag is a
> post-artifact convenience alias only. Historical stable-lock examples below
> are updated to the current candidate form; any reusable-trigger contract is
> superseded by a separate CI `workflow_dispatch` run.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generate one generic candidate-lock YAML whose root-level image variables are directly consumable by every pinned Kolla-Ansible release while preserving the validated multi-architecture manifest digest as explicit handoff metadata.

**Architecture:** Keep the validated publish summary as the sole source for candidate-lock data. Render a dual-contract document: root-level `*_image_full` values use the architecture-neutral deploy tag that Kolla-Ansible can parse, while a reserved `_kolla_candidate_lock` mapping records the stream, exact scope, deploy reference, manifest digest, immutable digest reference, and associated variables for every image. `openstack-infra-ops` verifies tag-to-digest identity before deployment; this repository still stops at the generic handoff artifact.

**Tech Stack:** Python 3 standard library, `unittest`, JSON-backed build/profile configuration, YAML-compatible text rendering, Markdown, Git, and `actionlint`.

## Global Constraints

- Preserve all seven configured streams and their exact Kolla-Ansible pins: `20.4.0`, `21.1.0`, and `22.0.0`.
- Root-level `*_image_full` values must be architecture-neutral
  `repository:stream-candidate-tag` references with no `-amd64`, `-arm64`, or
  `@sha256` suffix.
- `_kolla_candidate_lock.images` must bind every resolved deployment leaf to its validated `deploy_ref`, `manifest_digest`, `immutable_ref`, and `kolla_ansible_variables`.
- Keep candidate-lock eligibility exactly `deployment/all` (`profile=deployment` and `image=all`); core, single-image, partial, or invalid summaries remain ineligible.
- Keep the existing artifact path `artifacts/kolla-ansible-image-lock-<stream>.yml` and leave publish-plan, native evidence, publish-summary, and manifest formats unchanged.
- Do not alter the explicit non-dry-run approval gate, protected environment, native AMD64/ARM64 policy, or Ubuntu 24.04 publish scope.
- Add no external dependency, Kolla-Ansible fork, digest-derived alias tag, GHCR mutation, workflow dispatch, repository-variable change, environment lock, promotion, pointer, deployment, or rollback action.
- Preserve the repository boundary: `openstack-infra-ops` owns tag-to-digest verification at deployment time and every Dev/Stg/Prod concern.
- Do not rename the current branch.
- Keep exactly one local workspace commit by amending the existing commit only after all implementation and verification are complete; do not push or change PR state without separate user approval.

---

## File Map

- Modify `scripts/generate-lock.py`: render the dual-contract candidate-lock YAML from an already validated complete publish summary.
- Create `tests/fixtures/kolla-ansible-parse-image-contract.json`: pin the exact upstream `parse_image` source and module SHA-256 provenance for Kolla-Ansible `20.4.0`, `21.1.0`, and `22.0.0`.
- Modify `tests/test_lock_generation.py`: prove the old combined reference is incompatible with the pinned parser contract and verify every new root variable and metadata entry across all streams.
- Modify `tests/test_repository_boundary.py`: enforce the operator-facing dual contract and reject direct `*_image_full` digest references in current operational documentation.
- Modify `README.md`: explain the tag-consumption and immutable-metadata halves of the candidate lock.
- Modify `docs/publish.md`: document how Kolla-Ansible consumes the root variables and what downstream verification is mandatory.
- Modify `docs/build-readiness.md`: define the terminal artifact's digest-bound evidence and the handoff verification responsibility.
- Modify `docs/superpowers/specs/2026-07-13-kolla-multi-stream-ghcr-design.md`: replace the superseded direct-digest example with the approved dual contract.
- Modify `docs/superpowers/plans/2026-07-13-kolla-multi-stream-ghcr.md`: correct the earlier implementation examples so repository documentation does not prescribe an unsafe value.
- Preserve `docs/superpowers/specs/2026-07-14-kolla-compatible-candidate-lock-design.md`: this is the approved source specification.
- Modify this plan only to mark completed checkboxes while executing it.

### Task 1: Candidate-Lock Consumer Contract and Renderer

**Files:**

- Create: `tests/fixtures/kolla-ansible-parse-image-contract.json`
- Modify: `tests/test_lock_generation.py`
- Modify: `scripts/generate-lock.py`

**Interfaces:**

- Consumes: `render_lock(matrix: dict[str, Any], profile: dict[str, Any], stream: dict[str, Any], summary: dict[str, Any]) -> str` and the publish-summary fields already validated by `VALIDATE_PUBLISH_SUMMARY`.
- Consumes: versioned upstream parser fixtures whose exact source is executed in isolation by the regression test.
- Produces: the same UTF-8 YAML artifact path, with `_kolla_candidate_lock` metadata plus root-level Kolla-Ansible variables.
- Produces: `yaml_string(value: str) -> str`, a standard-library JSON-string encoder used for YAML-compatible quoted scalars.
- Preserves: CLI arguments, exit codes, complete-scope validation, duplicate-variable rejection, and trailing newline behavior.

- [x] **Step 1: Add versioned upstream parser fixtures and RED contract tests**

Create `tests/fixtures/kolla-ansible-parse-image-contract.json` with the exact `parse_image` method extracted from each pinned source distribution. The method source is identical in all three versions, while `module_sha256` identifies each complete upstream `ansible/module_utils/kolla_container_worker.py` file:

```json
{
  "schema_version": 1,
  "sources": {
    "378a902ce3ec0ed34256a5c35e82327de6655ce5d6026b92db2deba4164a8f9c": "def parse_image(self):\n        full_image = self.params.get('image')\n\n        if '/' in full_image:\n            registry, image = full_image.split('/', 1)\n        else:\n            image = full_image\n\n        if ':' in image:\n            return full_image.rsplit(':', 1)\n        else:\n            return full_image, 'latest'"
  },
  "versions": {
    "20.4.0": {
      "distribution": "kolla-ansible==20.4.0",
      "source_path": "ansible/module_utils/kolla_container_worker.py",
      "module_sha256": "3a22d2f70e8e3f3eea47be1b755ec5c37ed11d282e96db3094cd63846b01549f",
      "parse_image_sha256": "378a902ce3ec0ed34256a5c35e82327de6655ce5d6026b92db2deba4164a8f9c"
    },
    "21.1.0": {
      "distribution": "kolla-ansible==21.1.0",
      "source_path": "ansible/module_utils/kolla_container_worker.py",
      "module_sha256": "1c4251075d6ee4987b8fc7bd0429064ef42c905a141f9c863c57d1a0b822d7a0",
      "parse_image_sha256": "378a902ce3ec0ed34256a5c35e82327de6655ce5d6026b92db2deba4164a8f9c"
    },
    "22.0.0": {
      "distribution": "kolla-ansible==22.0.0",
      "source_path": "ansible/module_utils/kolla_container_worker.py",
      "module_sha256": "0cc53ffa96081cf6744bbe705652df381b3c4b4547728d01a471fbc0956ddfac",
      "parse_image_sha256": "378a902ce3ec0ed34256a5c35e82327de6655ce5d6026b92db2deba4164a8f9c"
    }
  }
}
```

Add `hashlib` and `re` imports, fixture constants, an isolated executor for the exact pinned source, a strict parser for the generated YAML subset, and an expected-object builder in `tests/test_lock_generation.py`:

```python
import hashlib
import re

PARSER_CONTRACT_PATH = (
    ROOT / "tests" / "fixtures" / "kolla-ansible-parse-image-contract.json"
)
PINNED_KOLLA_PARSER_MODULE_SHA256 = {
    "20.4.0": "3a22d2f70e8e3f3eea47be1b755ec5c37ed11d282e96db3094cd63846b01549f",
    "21.1.0": "1c4251075d6ee4987b8fc7bd0429064ef42c905a141f9c863c57d1a0b822d7a0",
    "22.0.0": "0cc53ffa96081cf6744bbe705652df381b3c4b4547728d01a471fbc0956ddfac",
}
ROOT_ASSIGNMENT_RE = re.compile(r'^([a-z0-9_]+): "([^"]+)"$')


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


def lock_assignments(lock: str) -> list[tuple[str, str]]:
    assignments = []
    for line in lock.splitlines():
        match = ROOT_ASSIGNMENT_RE.fullmatch(line)
        if match:
            assignments.append((match.group(1), match.group(2)))
    return assignments


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
```

Add these tests to `LockGenerationTest`:

```python
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
```

Update `test_complete_deployment_writes_every_resolved_variable_once` so its root-value assertions are:

```python
self.assertEqual(dict(assignments), expected_assignments(stream_id, summary))
for variable, value in assignments:
    self.assertRegex(variable, r"^[a-z0-9_]+$")
    self.assertNotIn("@", value)
    self.assertNotIn("-amd64", value)
    self.assertNotIn("-arm64", value)
```

- [x] **Step 2: Run the focused tests and capture RED**

Run:

```bash
python3 -m unittest \
  tests.test_lock_generation.LockGenerationTest.test_complete_deployment_writes_every_resolved_variable_once \
  tests.test_lock_generation.LockGenerationTest.test_tag_digest_value_is_incompatible_with_pinned_kolla_parser \
  tests.test_lock_generation.LockGenerationTest.test_generated_lock_structurally_matches_summary_and_pinned_parser \
  -v
```

Expected: the exact versioned upstream-parser fixture test passes, while the generated-lock tests fail because root variables still contain `@sha256:` and the strict structural parser receives a root assignment where `_kolla_candidate_lock:` is required. Save the command and failure summary for the final RED evidence.

- [x] **Step 3: Implement the minimal dual-contract renderer**

Update the module description and add the scalar helper in `scripts/generate-lock.py`:

```python
"""Generate a generic digest-bound candidate lock from a publish summary."""


def yaml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)
```

Replace the rendering block after publish-summary validation with:

```python
images = {image["image"]: image for image in summary["images"]}
lines = [
    "# Generated by scripts/generate-lock.py from a complete publish summary.",
    "# Root-level *_image_full values are Kolla-Ansible-compatible deploy tags.",
    "# _kolla_candidate_lock binds those tags to immutable manifest digests.",
    "_kolla_candidate_lock:",
    "  schema_version: 1",
    f"  stream: {yaml_string(stream['id'])}",
    "  scope:",
    '    profile: "deployment"',
    '    image: "all"',
    f"    image_count: {expected_scope['image_count']}",
    "  images:",
]
assignment_lines: list[str] = []
emitted_variables: set[str] = set()

for profile_image in profile["images"]:
    image_summary = images[profile_image["name"]]
    deploy_ref = image_summary["deploy_ref"]
    manifest_digest = image_summary["manifest_digest"]
    repository, _deploy_tag = deploy_ref.rsplit(":", 1)
    immutable_ref = f"{repository}@{manifest_digest}"
    variables = profile_image["kolla_ansible_variables"]

    lines.extend(
        (
            f"    {yaml_string(profile_image['name'])}:",
            f"      deploy_ref: {yaml_string(deploy_ref)}",
            f"      manifest_digest: {yaml_string(manifest_digest)}",
            f"      immutable_ref: {yaml_string(immutable_ref)}",
            "      kolla_ansible_variables:",
        )
    )
    for variable in variables:
        if variable in emitted_variables:
            raise ValueError(
                f"resolved profile contains duplicate variable: {variable}"
            )
        emitted_variables.add(variable)
        lines.append(f"        - {yaml_string(variable)}")
        assignment_lines.append(f"{variable}: {yaml_string(deploy_ref)}")

lines.extend(assignment_lines)
return "\n".join(lines) + "\n"
```

Do not add a YAML package: `json.dumps` produces quoted scalars accepted by YAML while every mapping/list line remains explicitly rendered.

- [x] **Step 4: Run focused GREEN tests**

Run the same three-test command from Step 2.

Expected: `Ran 3 tests` followed by `OK`. The generated root assignments equal the validated deploy refs, and every summary image has exact digest metadata.

- [x] **Step 5: Run all candidate-lock and publish-summary contract tests**

Run:

```bash
python3 -m unittest \
  tests.test_publish_summary_validation \
  tests.test_lock_generation \
  -v
```

Expected: exit code `0` and final status `OK`; malformed summaries, partial scopes, duplicate keys, stream-specific aliases, and exact coverage remain enforced.

### Task 2: Operator Documentation and Repository Boundary

**Files:**

- Modify: `tests/test_repository_boundary.py`
- Modify: `README.md`
- Modify: `docs/publish.md`
- Modify: `docs/build-readiness.md`
- Modify: `docs/superpowers/specs/2026-07-13-kolla-multi-stream-ghcr-design.md`
- Modify: `docs/superpowers/plans/2026-07-13-kolla-multi-stream-ghcr.md`

**Interfaces:**

- Consumes: the `_kolla_candidate_lock` schema and tag-only root assignments produced by Task 1.
- Produces: a single operator contract stating that Kolla-Ansible receives tag-only `*_image_full` values and `openstack-infra-ops` verifies each tag against its recorded digest before deployment.
- Preserves: architecture-neutral multi-arch selection, generic handoff, protected publication, secrets, environment, and promotion boundaries.

- [x] **Step 1: Change documentation tests first**

Replace the direct-digest assertion in `test_publish_doc_has_architecture_neutral_consumption_example` with:

```python
self.assertRegex(
    document,
    re.compile(
        r'(?m)^nova_compute_image_full:\s*"ghcr\.io/supergate-hub/'
        r'kolla-container-images/nova-compute:2025\.1-rocky-9-'
        r'candidate-123456789-1"\s*$'
    ),
)
self.assertNotRegex(
    document,
    r"(?m)^[a-z0-9_]+_image_full:.*@sha256:",
)
```

Add a new repository-boundary test:

```python
def test_candidate_lock_docs_define_dual_contract_and_external_verification(
    self,
) -> None:
    for path in (README, PUBLISH_DOC, READINESS_DOC):
        with self.subTest(path=path.relative_to(ROOT)):
            document = read_text(path)
            self.assert_tokens(
                document,
                "_kolla_candidate_lock",
                "deploy_ref",
                "manifest_digest",
                "immutable_ref",
                "openstack-infra-ops",
                "before deployment",
            )
            self.assertNotRegex(
                document,
                r"(?m)^[a-z0-9_]+_image_full:.*@sha256:",
            )
```

- [x] **Step 2: Run the documentation tests and capture RED**

Run:

```bash
python3 -m unittest \
  tests.test_repository_boundary.RepositoryBoundaryTest.test_publish_doc_has_architecture_neutral_consumption_example \
  tests.test_repository_boundary.RepositoryBoundaryTest.test_candidate_lock_docs_define_dual_contract_and_external_verification \
  -v
```

Expected: failures identify the old `nova_compute_image_full: ...@sha256:` example and missing dual-contract/downstream-verification language.

- [x] **Step 3: Update the README candidate-lock contract**

Change the introduction from “digest-pinned candidate lock” to “digest-bound candidate lock.” Replace the `Image and lock outputs` candidate-lock paragraph and example with this structure:

```yaml
_kolla_candidate_lock:
  schema_version: 1
  stream: "2025.1-rocky-9"
  scope:
    profile: "deployment"
    image: "all"
    image_count: 63
  images:
    "keystone":
      deploy_ref: "ghcr.io/supergate-hub/kolla-container-images/keystone:2025.1-rocky-9-candidate-123456789-1"
      manifest_digest: "sha256:<multi-arch-manifest-digest>"
      immutable_ref: "ghcr.io/supergate-hub/kolla-container-images/keystone@sha256:<multi-arch-manifest-digest>"
      kolla_ansible_variables:
        - "keystone_image_full"
keystone_image_full: "ghcr.io/supergate-hub/kolla-container-images/keystone:2025.1-rocky-9-candidate-123456789-1"
```

State immediately after the example:

```text
Kolla-Ansible consumes the root-level tag-only variables. Before deployment,
openstack-infra-ops must verify that each deploy_ref resolves to the recorded
manifest_digest and that immutable_ref returns the same manifest bytes. The
lock selects neither an architecture nor an environment.
```

- [x] **Step 4: Update publish and readiness operations guidance**

In `docs/publish.md`, replace the extra-vars example with:

```yaml
# generated candidate lock supplied as an extra-vars file
_kolla_candidate_lock:
  # Digest-bound supply evidence; Kolla-Ansible roles ignore this reserved data.
  images:
    "nova-compute":
      deploy_ref: "ghcr.io/supergate-hub/kolla-container-images/nova-compute:2025.1-rocky-9-candidate-123456789-1"
      manifest_digest: "sha256:<multi-arch-manifest-digest>"
      immutable_ref: "ghcr.io/supergate-hub/kolla-container-images/nova-compute@sha256:<multi-arch-manifest-digest>"
nova_compute_image_full: "ghcr.io/supergate-hub/kolla-container-images/nova-compute:2025.1-rocky-9-candidate-123456789-1"
```

Add this required consumption sequence in `docs/publish.md`:

```text
Before deployment, openstack-infra-ops resolves every deploy_ref, compares its
manifest bytes and digest with manifest_digest and immutable_ref, and only then
passes the root-level variables to Kolla-Ansible. The pinned Kolla-Ansible
releases do not enforce digest identity themselves; a successful extra-vars
load is not a substitute for this verification.
```

Replace the terminal lock paragraph in `docs/build-readiness.md` with:

```text
The lock contains tag-only architecture-neutral *_image_full variables for
Kolla-Ansible plus a reserved _kolla_candidate_lock mapping. For every image,
that mapping records deploy_ref, manifest_digest, immutable_ref, and the
associated Kolla-Ansible variables. It is a generic, digest-bound candidate
for handoff to openstack-infra-ops. Before deployment, that repository must
verify that each deploy tag still resolves to the recorded digest and bytes.
It is not an environment lock, promotion pointer, or deployment action.
```

- [x] **Step 5: Correct superseded examples in the earlier design and plan**

In `docs/superpowers/specs/2026-07-13-kolla-multi-stream-ghcr-design.md`, replace the direct `nova_compute_image_full: ...@sha256:` example with the same metadata-plus-tag shape used in the approved 2026-07-14 design and state that the newer candidate-lock design supersedes the direct-digest consumption shape.

In `docs/superpowers/plans/2026-07-13-kolla-multi-stream-ghcr.md`, replace both direct `nova_compute_image_full: ...@sha256:` examples with:

```yaml
_kolla_candidate_lock:
  images:
    "nova-compute":
      deploy_ref: "ghcr.io/supergate-hub/kolla-container-images/nova-compute:2025.1-rocky-9-candidate-123456789-1"
      manifest_digest: "sha256:<multi-arch-manifest-digest>"
      immutable_ref: "ghcr.io/supergate-hub/kolla-container-images/nova-compute@sha256:<multi-arch-manifest-digest>"
nova_compute_image_full: "ghcr.io/supergate-hub/kolla-container-images/nova-compute:2025.1-rocky-9-candidate-123456789-1"
```

Keep immutable native child references such as `repository@sha256:<amd64-child-digest>` unchanged because those are registry evidence operations, not Kolla-Ansible variables.

- [x] **Step 6: Run documentation GREEN and focused boundary checks**

Run:

```bash
python3 -m unittest tests.test_repository_boundary -v
```

Expected: exit code `0` and final status `OK`; architecture, service, approval, generic-handoff, secret, and no-environment-state contracts continue to pass.

Run:

```bash
if rg -n '^[[:space:]]*[a-z0-9_]+_image_full:.*@sha256:' \
  README.md docs scripts tests; then
  exit 1
fi
```

Expected: exit code `0` with no matches. Explanatory prose about why the legacy shape fails may remain, but no documentation or generated example may prescribe it as a Kolla-Ansible variable.

### Task 3: Full Verification, Independent Review, and Single-Commit Finalization

**Files:**

- Verify: every tracked change against `origin/main`
- Amend: the existing single local commit with the implementation, tests, docs, approved spec, and this plan

**Interfaces:**

- Consumes: the renderer, tests, and documentation completed in Tasks 1 and 2.
- Produces: one verified local commit on the existing workspace branch.
- Does not produce: a push, PR-state change, workflow run, GHCR package, repository variable, or environment deployment.

- [x] **Step 1: Validate every JSON document and resolved configuration**

Run:

```bash
while IFS= read -r file; do
  python3 -m json.tool "$file" >/dev/null || exit 1
done < <(rg --files -g '*.json')
python3 scripts/validate-config.py
```

Expected: both commands exit `0`; configuration validation reports success for all seven streams and both native architectures.

- [x] **Step 2: Render representative dry-run plans without publishing**

Run:

```bash
python3 scripts/plan-publish.py --stream 2025.1-rocky-9 --profile core --dry-run >/dev/null
python3 scripts/plan-publish.py --stream 2025.1-rocky-9 --profile core --image keystone --dry-run >/dev/null
python3 scripts/plan-publish.py --stream 2025.1-rocky-9 --profile deployment --dry-run >/dev/null
python3 scripts/plan-publish.py --stream 2025.1-ubuntu-noble --profile deployment --dry-run >/dev/null
```

Expected: all four commands exit `0`; no workflow is dispatched and no registry mutation occurs.

- [x] **Step 3: Run the full unit and workflow-static suites**

Run:

```bash
python3 -m unittest discover -s tests -v
actionlint .github/workflows/*.yml
```

Expected: the unit suite ends with `OK`, including the newly added RED/GREEN regressions, and `actionlint` exits `0` without output. Record the exact final unit-test count.

- [x] **Step 4: Run namespace, unsafe-reference, and whitespace checks**

Run:

```bash
legacy_owner='supergate-''jhbyun'
if rg -n "${legacy_owner}/kolla-container-images" \
  --glob '!.git/**' --glob '!.context/**' .; then
  exit 1
fi
if rg -n '^[[:space:]]*[a-z0-9_]+_image_full:.*@sha256:' \
  README.md docs scripts tests; then
  exit 1
fi
git diff --check
```

Expected: both searches exit through the no-match path, `git diff --check` exits `0`, and none prints a violation.

- [x] **Step 5: Stage the exact scope and request an independent implementation review**

Run:

```bash
git add \
  scripts/generate-lock.py \
  tests/fixtures/kolla-ansible-parse-image-contract.json \
  tests/test_lock_generation.py \
  tests/test_repository_boundary.py \
  README.md \
  docs/build-readiness.md \
  docs/publish.md \
  docs/superpowers/specs/2026-07-13-kolla-multi-stream-ghcr-design.md \
  docs/superpowers/specs/2026-07-14-kolla-compatible-candidate-lock-design.md \
  docs/superpowers/plans/2026-07-13-kolla-multi-stream-ghcr.md \
  docs/superpowers/plans/2026-07-14-kolla-compatible-candidate-lock.md
git status --short --branch
git diff --cached --stat origin/main
git diff --cached origin/main -- \
  scripts/generate-lock.py \
  tests/fixtures/kolla-ansible-parse-image-contract.json \
  tests/test_lock_generation.py \
  tests/test_repository_boundary.py \
  README.md \
  docs/build-readiness.md \
  docs/publish.md \
  docs/superpowers/specs/2026-07-13-kolla-multi-stream-ghcr-design.md \
  docs/superpowers/specs/2026-07-14-kolla-compatible-candidate-lock-design.md \
  docs/superpowers/plans/2026-07-13-kolla-multi-stream-ghcr.md \
  docs/superpowers/plans/2026-07-14-kolla-compatible-candidate-lock.md
```

Expected: every listed path is staged, changes are limited to the listed candidate-lock code, tests, and documentation, and the cached diff includes both newly created files. Give an independent reviewer the approved design plus this exact cached diff and require resolution of every Critical or Important finding before proceeding.

- [x] **Step 6: Check the reviewed staged diff and amend the existing commit once**

Run:

```bash
git diff --cached --check
git commit --amend --no-edit
```

Expected: staged diff check passes and Git rewrites the existing `feat: add multi-stream organization GHCR pipeline` commit; no second local commit is created.

- [x] **Step 7: Re-run completion evidence against the amended commit**

Run:

```bash
git status --short --branch
git log -1 --oneline
git rev-list --count origin/main..HEAD
python3 -m unittest discover -s tests -v
actionlint .github/workflows/*.yml
git diff --check origin/main...HEAD
```

Expected: worktree is clean, the commit subject remains `feat: add multi-stream organization GHCR pipeline`, the branch contains exactly one commit over `origin/main`, unit tests end in `OK`, `actionlint` is silent, and the committed diff check passes.

- [x] **Step 8: Report without pushing or changing PR state**

Report the changed files and dual-contract behavior, captured RED failure, GREEN/full test count, representative deploy and immutable refs, unchanged approval gate, required GitHub/GHCR manual setup, maintained repository boundary, and the remaining mutable-tag time-of-check/time-of-use risk. State explicitly that no publish, workflow dispatch, repository-variable change, push, or PR transition occurred. Ask for separate approval before any `git push --force-with-lease` or `gh pr ready` operation.
