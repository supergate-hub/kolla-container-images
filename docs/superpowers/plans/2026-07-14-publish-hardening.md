# Kolla Publish Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the organization GHCR pipeline publish run-unique AMD64/ARM64 candidate images, prove the current Kolla invocation built the complete frozen scope, emit a candidate-bound generic lock, and defer mutable stream aliases until after the validated candidate artifact is safely uploaded.

**Architecture:** A trusted `<github.run_id>-<github.run_attempt>` candidate ID flows through the frozen plan, approval recomputation, native child tags, publish summary, and candidate lock; local read-only plans use `local-dry-run`. Native jobs delete and strictly validate Kolla's exact JSON summary before accepting remote image evidence, while the finalizer validates run-unique multi-architecture manifests and uploads their artifacts before updating convenience stream aliases from immutable digests. Workflow credentials, Docker endpoint checks, and third-party Actions fail closed before registry mutation.

**Tech Stack:** Python 3.12 standard library, Kolla `20.4.0`/`21.1.0`/`22.0.0`, Docker Engine and Buildx, GitHub Actions YAML, GHCR, JSON, YAML-compatible lock rendering, `unittest`, `actionlint`, and Git.

## Global Constraints

- Preserve all seven publish-enabled streams, including Rocky Linux 9/10 and Ubuntu 24.04 (`noble`), with exact Kolla and Kolla-Ansible pins `20.4.0`, `21.1.0`, and `22.0.0`.
- Preserve native AMD64 and native ARM64 builds and image smoke; QEMU output is never ARM64 publication or deployment-approval evidence.
- A workflow candidate ID is exactly `<github.run_id>-<github.run_attempt>` with two positive decimal components and no leading zero; local read-only planning defaults to exactly `local-dry-run`.
- Expose publication through `workflow_dispatch` only. A CI pipeline starts a separate dispatch run through the Actions API or `gh workflow run`; do not use `workflow_call`, because reusable calls share the caller run identity and artifact namespace. Qualify every uploaded/downloaded artifact name with the exact candidate ID so workflow reruns cannot collide with a previous attempt.
- Candidate child tags are `<stream>-candidate-<candidate-id>-<arch>` and candidate multi-architecture tags are `<stream>-candidate-<candidate-id>`.
- Candidate locks and root-level `*_image_full` values use the run-unique candidate multi-architecture tag; stable `<stream>` tags are convenience aliases and never lock inputs.
- Preserve the explicit non-dry-run scope variable, exact count-bearing approval phrase, and protected `ghcr-publish` environment gate; the approval phrase does not include candidate ID.
- Require Kolla's current summary to prove the exact planned parent/leaf union before remote architecture-tag inspection, native smoke, or evidence generation.
- Require the active native Docker server to be Linux, match `x86_64` or `aarch64`, and use a local Unix socket after Buildx setup and before registry login.
- Pin every `uses:` reference in `.github/workflows/publish.yml` and `.github/workflows/validate.yml` to the reviewed 40-character commit SHA and retain a semantic major comment.
- Set `persist-credentials: false` on every checkout, including both jobs with `packages: write`.
- Hard-cap the total online runner pool eligible for the `kolla-build` label at four; excess cross-stream jobs queue, while one workflow run still has `max-parallel: 2`.
- Upload the complete `publish-<stream>-<candidate-id>` candidate artifact before sequential convenience stream-alias writes; alias failure fails the run but does not invalidate the uploaded candidate lock.
- Keep this repository's terminal responsibility at the generic candidate lock handoff. Add no Dev/Stg/Prod lock, promotion, environment pointer, site validation, deployment, rollback, or environment secret logic.
- Add no external dependency and perform no real GHCR publish, workflow dispatch, repository-variable/environment change, push, PR creation, or PR state transition.
- Do not rename the current branch.
- Preserve exactly one local commit over `origin/main`: use `git commit --amend --no-edit` for each completed checkpoint and verify the final commit count is one.

---

## File Map

- Modify `config/build-matrix.json`: declare stable, candidate, and candidate-architecture tag templates.
- Modify `scripts/profile_resolver.py`: validate candidate IDs and render candidate tags separately from stable stream tags.
- Modify `scripts/validate-config.py`: fail closed on the exact extended tag policy and rendered examples.
- Modify `scripts/plan-publish.py`: freeze candidate identity, candidate child/deploy refs, and separate stable `stream_ref` values.
- Modify `scripts/validate-publish-approval.py`: bind full-plan recomputation to the trusted workflow candidate ID.
- Create `tests/fixtures/kolla-build-summary-contract.json`: record the verified common summary schema, exact extracted method source as a JSON string, and source provenance for all three pinned Kolla releases.
- Create `scripts/validate-kolla-build-summary.py`: strictly validate the current Kolla JSON result against one architecture unit in the frozen plan.
- Create `tests/test_kolla_build_summary_validation.py`: cover exact success and every incomplete/malformed/stale-result failure mode.
- Modify `.github/workflows/publish.yml`: pass trusted candidate identity, disable checkout credentials, validate native Docker and current Kolla results, create candidate manifests, upload candidate artifacts, then update stable aliases.
- Modify `.github/workflows/validate.yml`: pin checkout and disable persisted checkout credentials.
- Modify `scripts/validate-publish-summary.py`: validate candidate identity and candidate refs instead of stable stream refs.
- Modify `scripts/generate-lock.py`: bind summary validation to the expected candidate ID while retaining generic `deployment/all` output only.
- Modify `tests/test_config_validation.py`, `tests/test_profile_resolution.py`, `tests/test_plan_publish.py`, and `tests/test_publish_approval.py`: enforce candidate tag and frozen-plan contracts.
- Modify `tests/test_publish_summary_validation.py` and `tests/test_lock_generation.py`: enforce candidate-bound summaries and locks across all seven streams.
- Modify `tests/test_publish_workflow.py`: enforce dispatch-only isolation, exact Action pins, checkout policy, ephemeral Docker credentials, trusted candidate flow, Docker/Kolla ordering, and artifact-before-alias ordering.
- Modify `tests/test_repository_boundary.py`: enforce candidate examples, four-runner capacity, and the unchanged repository boundary.
- Modify `README.md`, `docs/publish.md`, and `docs/build-readiness.md`: document candidate refs, Kolla proof, Docker trust, alias recovery, capacity, and manual prerequisites.
- Modify `docs/superpowers/specs/2026-07-13-kolla-multi-stream-ghcr-design.md`, `docs/superpowers/plans/2026-07-13-kolla-multi-stream-ghcr.md`, `docs/superpowers/specs/2026-07-14-kolla-compatible-candidate-lock-design.md`, and `docs/superpowers/plans/2026-07-14-kolla-compatible-candidate-lock.md`: mark stable-tag lock examples as superseded and use the approved run-unique candidate contract.
- Modify `docs/superpowers/specs/2026-07-14-publish-hardening-design.md`: append the post-review dispatch isolation, attempt-qualified artifact, rerun, and Docker credential-cleanup decisions without rewriting the approved core design.
- Modify this plan only to mark completed checkboxes during execution.

### Task 1: Candidate Identity, Tag Policy, Frozen Plan, and Approval Binding

**Files:**

- Modify: `config/build-matrix.json:18-22`
- Modify: `scripts/profile_resolver.py:1-116`
- Modify: `scripts/validate-config.py:64-72,299-358`
- Modify: `scripts/plan-publish.py:18-265`
- Modify: `scripts/validate-publish-approval.py:21-114`
- Modify: `tests/test_config_validation.py:150-205`
- Modify: `tests/test_profile_resolution.py:25-50,220-236`
- Modify: `tests/test_plan_publish.py:40-540`
- Modify: `tests/test_publish_approval.py:40-560`

**Interfaces:**

- Produces: `LOCAL_DRY_RUN_CANDIDATE_ID: str = "local-dry-run"`.
- Produces: `validate_candidate_id(candidate_id: str, *, allow_local: bool = True) -> str`.
- Produces: `render_tag(matrix: dict[str, Any], stream: dict[str, Any], arch: str | None = None) -> str` as the compatibility renderer for stable stream/architecture tokens; no live publish path uses its architecture form after this task.
- Produces: `render_candidate_tag(matrix: dict[str, Any], stream: dict[str, Any], candidate_id: str, arch: str | None = None) -> str`.
- Produces: `build_plan(matrix: dict[str, Any], profile: dict[str, Any], stream: dict[str, Any], image_filter: str | None = None, candidate_id: str = LOCAL_DRY_RUN_CANDIDATE_ID) -> dict[str, Any]` with top-level `candidate_id`, candidate `deploy_ref`, candidate architecture refs, and per-image stable `stream_ref`.
- Consumes: `validate-publish-approval.py --publish-plan PATH --expected-candidate-id ID`; the expected ID is trusted workflow context, never a workflow input.
- Preserves: the three approval variables and exact count-bearing phrase generated by `approval_requirement()`.

- [x] **Step 1: Write RED tag-policy, plan-shape, and approval-binding tests**

Update the matrix expectation in `tests/test_config_validation.py`:

```python
self.assertEqual(
    self.matrix["tag_policy"],
    {
        "deploy_tag_template": "{release}-{distro}-{tag_token}",
        "candidate_tag_template": (
            "{release}-{distro}-{tag_token}-candidate-{candidate_id}"
        ),
        "candidate_arch_tag_template": (
            "{release}-{distro}-{tag_token}-candidate-{candidate_id}-{arch}"
        ),
    },
)
```

Update the synthetic matrix in `tests/test_profile_resolution.py` to the same
three keys. Extend `test_malformed_tag_templates_fail_closed` with these exact
field substitutions so every new formatter dimension fails closed:

```python
from scripts.profile_resolver import (
    render_candidate_tag,
    render_tag,
    validate_candidate_id,
)
```

Merge these names into the module's existing resolver import rather than
adding a duplicate import block.

```python
cases = (
    ("deploy_tag_template", "{}", "deploy_tag_template fields"),
    (
        "candidate_tag_template",
        "{release}-{distro}-{tag_token}",
        "candidate_tag_template fields",
    ),
    (
        "candidate_arch_tag_template",
        "{release}-{distro}-{tag_token}-candidate-{candidate_id}",
        "candidate_arch_tag_template fields",
    ),
)
for field, template, expected_error in cases:
    with self.subTest(field=field):
        matrix = copy.deepcopy(self.matrix)
        matrix["tag_policy"][field] = template
        errors: list[str] = []
        validate_matrix(matrix, errors)
        self.assertTrue(
            any(expected_error in error for error in errors),
            errors,
        )
```

Replace the profile tag test with exact stable/candidate separation:

```python
def test_render_stream_and_candidate_tags_are_explicitly_separate(self) -> None:
    self.assertEqual(
        render_tag(self.matrix, self.ubuntu_2026),
        "2026.1-ubuntu-noble",
    )
    self.assertEqual(
        render_tag(self.matrix, self.ubuntu_2026, "arm64"),
        "2026.1-ubuntu-noble-arm64",
    )
    self.assertEqual(
        render_candidate_tag(
            self.matrix, self.ubuntu_2026, "123456789-1"
        ),
        "2026.1-ubuntu-noble-candidate-123456789-1",
    )
    self.assertEqual(
        render_candidate_tag(
            self.matrix, self.ubuntu_2026, "123456789-1", "arm64"
        ),
        "2026.1-ubuntu-noble-candidate-123456789-1-arm64",
    )

def test_candidate_id_validation_rejects_non_run_shapes(self) -> None:
    self.assertEqual(validate_candidate_id("local-dry-run"), "local-dry-run")
    self.assertEqual(validate_candidate_id("123456789-1"), "123456789-1")
    for value in ("", "0-1", "1-0", "01-1", "1-01", "1", "1-a", True):
        with self.subTest(value=value):
            with self.assertRaises(ValueError):
                validate_candidate_id(value)
    with self.assertRaisesRegex(ValueError, "workflow candidate ID"):
        validate_candidate_id("local-dry-run", allow_local=False)
```

Extend `plan_command()` and `run_plan()` in `tests/test_plan_publish.py` exactly as follows, and update existing deploy/architecture/manifest expectations to use `TEST_CANDIDATE_ID` candidate tags while asserting each stable `stream_ref` separately:

```python
TEST_CANDIDATE_ID = "123456789-1"


def expected_candidate_tag(stream: str, arch: str | None = None) -> str:
    tag = f"{stream}-candidate-{TEST_CANDIDATE_ID}"
    return f"{tag}-{arch}" if arch else tag


def expected_ref(image: str, stream: str, arch: str | None = None) -> str:
    return (
        "ghcr.io/supergate-hub/kolla-container-images/"
        f"{image}:{expected_candidate_tag(stream, arch)}"
    )


def plan_command(
    *,
    stream: str = "2025.1-rocky-9",
    profile: str = "core",
    image: str | None = None,
    candidate_id: str | None = TEST_CANDIDATE_ID,
    dry_run: bool = True,
) -> list[str]:
    command = [
        sys.executable,
        str(PLAN_PUBLISH),
        "--stream", stream,
        "--profile", profile,
    ]
    if image is not None:
        command.extend(["--image", image])
    if candidate_id is not None:
        command.extend(["--candidate-id", candidate_id])
    if dry_run:
        command.append("--dry-run")
    return command


def run_plan(
    *,
    stream: str = "2025.1-rocky-9",
    profile: str = "core",
    image: str | None = None,
    candidate_id: str | None = TEST_CANDIDATE_ID,
) -> dict:
    result = subprocess.run(
        plan_command(
            stream=stream,
            profile=profile,
            image=image,
            candidate_id=candidate_id,
        ),
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )
    return json.loads(result.stdout)


def test_local_default_and_explicit_workflow_candidate_refs(self) -> None:
    local = run_plan(image="keystone", candidate_id=None)
    live = run_plan(image="keystone", candidate_id=TEST_CANDIDATE_ID)

    self.assertEqual(local["candidate_id"], "local-dry-run")
    self.assertEqual(
        local["images"][0]["deploy_ref"],
        "ghcr.io/supergate-hub/kolla-container-images/keystone:"
        "2025.1-rocky-9-candidate-local-dry-run",
    )
    self.assertEqual(live["candidate_id"], TEST_CANDIDATE_ID)
    image = live["images"][0]
    self.assertEqual(
        image["deploy_ref"],
        "ghcr.io/supergate-hub/kolla-container-images/keystone:"
        "2025.1-rocky-9-candidate-123456789-1",
    )
    self.assertEqual(
        image["stream_ref"],
        "ghcr.io/supergate-hub/kolla-container-images/keystone:2025.1-rocky-9",
    )
    self.assertEqual(
        [entry["arch_ref"] for entry in image["architectures"]],
        [
            "ghcr.io/supergate-hub/kolla-container-images/keystone:"
            "2025.1-rocky-9-candidate-123456789-1-amd64",
            "ghcr.io/supergate-hub/kolla-container-images/keystone:"
            "2025.1-rocky-9-candidate-123456789-1-arm64",
        ],
    )

def test_invalid_candidate_id_is_rejected(self) -> None:
    result = subprocess.run(
        plan_command(candidate_id="01-1"),
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    self.assertEqual(result.returncode, 2)
    self.assertIn("candidate ID", result.stderr)


def test_all_streams_use_candidate_build_and_deploy_tags(self) -> None:
    for stream_id in STREAM_IDS:
        with self.subTest(stream=stream_id):
            plan = run_plan(stream=stream_id, image="keystone")
            image = plan["images"][0]
            candidate_tag = f"{stream_id}-candidate-{TEST_CANDIDATE_ID}"
            self.assertEqual(plan["candidate_id"], TEST_CANDIDATE_ID)
            self.assertEqual(image["deploy_tag"], candidate_tag)
            self.assertTrue(image["deploy_ref"].endswith(f":{candidate_tag}"))
            self.assertTrue(image["stream_ref"].endswith(f":{stream_id}"))
            for architecture in plan["build"]["architectures"]:
                arch = architecture["arch"]
                arch_tag = f"{candidate_tag}-{arch}"
                command = architecture["commands"]["kolla_build_push"]
                self.assertEqual(architecture["arch_tag"], arch_tag)
                self.assertEqual(option_value(command, "--tag"), arch_tag)
                self.assertTrue(
                    all(entry["arch_ref"].endswith(f":{arch_tag}")
                        for entry in architecture["parents"])
                )
                self.assertTrue(
                    all(entry["arch_ref"].endswith(f":{arch_tag}")
                        for entry in architecture["images"])
                )
```

For every pre-existing assertion in this test module, replace the old
`f"{stream_id}-{arch}"` architecture tag with
`expected_candidate_tag(stream_id, arch)`, replace the old stream deploy ref
with `expected_ref(image_name, stream_id)`, and add this exact stable alias
assertion inside the per-image loop:

```python
self.assertEqual(
    image["stream_ref"],
    "ghcr.io/supergate-hub/kolla-container-images/"
    f"{image_name}:{stream_id}",
)
self.assertEqual(image["expected_ghcr_ref"], expected_ref(image_name, stream_id))
self.assertEqual(
    image["manifest_metadata_file"],
    f"artifacts/manifests/{image_name}-{expected_candidate_tag(stream_id)}.json",
)
```

Change `generate_plan()` and `run_validator()` in `tests/test_publish_approval.py` to use this exact candidate binding:

```python
TEST_CANDIDATE_ID = "123456789-1"


def generate_plan(
    *,
    stream: str = "2025.1-rocky-9",
    profile: str = "core",
    image: str | None = None,
    candidate_id: str = TEST_CANDIDATE_ID,
) -> dict:
    command = [
        sys.executable,
        str(PLAN_PUBLISH),
        "--stream", stream,
        "--profile", profile,
        "--candidate-id", candidate_id,
        "--dry-run",
    ]
    if image is not None:
        command.extend(["--image", image])
    result = subprocess.run(
        command,
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )
    return json.loads(result.stdout)


def run_validator(
    plan_path: Path,
    *,
    approval: str | None,
    variables: dict[str, str] | None = None,
    expected_candidate_id: str = TEST_CANDIDATE_ID,
    extra_args: list[str] | None = None,
    publish_plan_option: str = "--publish-plan",
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    for name in (*APPROVAL_VARIABLES, "APPROVAL"):
        env.pop(name, None)
    if approval is not None:
        env["APPROVAL"] = approval
    if variables:
        env.update(variables)
    return subprocess.run(
        [
            sys.executable,
            str(VALIDATE_APPROVAL),
            publish_plan_option,
            str(plan_path),
            "--expected-candidate-id",
            expected_candidate_id,
            *(extra_args or []),
        ],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
    )


def test_trusted_candidate_id_must_match_frozen_plan(self) -> None:
    plan = self.plan("2025.1-rocky-9", "core", "keystone")
    path = write_plan(self.plan_directory, "candidate-mismatch", plan)
    result = run_validator(
        path,
        approval=expected_phrase("2025.1-rocky-9", "core", "keystone", 1),
        variables={"ALLOW_GHCR_PUBLISH": "true"},
        expected_candidate_id="123456789-2",
    )
    self.assertEqual(result.returncode, 1)
    self.assertIn("candidate ID", result.stderr)

def test_local_candidate_id_cannot_authorize_publication(self) -> None:
    plan = generate_plan(candidate_id="local-dry-run")
    path = write_plan(self.plan_directory, "local-candidate", plan)
    result = run_validator(
        path,
        approval=expected_phrase("2025.1-rocky-9", "core", "all", 21),
        variables={"ALLOW_GHCR_FULL_CORE_PUBLISH": "true"},
        expected_candidate_id="local-dry-run",
    )
    self.assertEqual(result.returncode, 1)
    self.assertIn("workflow candidate ID", result.stderr)
```

Update the direct module call in
`test_review_rejects_stream_disabled_in_repository_matrix` to:

```python
validator.recompute_requirement(
    self.plan("2025.1-rocky-9", "core", "keystone"),
    TEST_CANDIDATE_ID,
)
```

- [x] **Step 2: Run focused tests and capture RED evidence**

Run:

```bash
python3 -m unittest \
  tests.test_config_validation \
  tests.test_profile_resolution \
  tests.test_plan_publish \
  tests.test_publish_approval -v
```

Expected: failures show missing candidate templates/functions/fields, old stable architecture refs, and the unrecognized `--expected-candidate-id` option. Save the command, failing test names, and failure total in `.context/publish-hardening-red-task1.txt`.

- [x] **Step 3: Implement the exact candidate tag policy and resolver API**

Set `config/build-matrix.json` tag policy to:

```json
"tag_policy": {
  "deploy_tag_template": "{release}-{distro}-{tag_token}",
  "candidate_tag_template": "{release}-{distro}-{tag_token}-candidate-{candidate_id}",
  "candidate_arch_tag_template": "{release}-{distro}-{tag_token}-candidate-{candidate_id}-{arch}"
}
```

In `scripts/profile_resolver.py`, import `re` and replace the overloaded renderer with:

```python
LOCAL_DRY_RUN_CANDIDATE_ID = "local-dry-run"
CANDIDATE_ID_RE = re.compile(r"^[1-9][0-9]*-[1-9][0-9]*$")


def validate_candidate_id(
    candidate_id: str,
    *,
    allow_local: bool = True,
) -> str:
    if type(candidate_id) is not str:
        raise ValueError("candidate ID must be a string")
    if allow_local and candidate_id == LOCAL_DRY_RUN_CANDIDATE_ID:
        return candidate_id
    if not CANDIDATE_ID_RE.fullmatch(candidate_id):
        expectation = "a workflow candidate ID <run_id>-<run_attempt>"
        if allow_local:
            expectation += f" or {LOCAL_DRY_RUN_CANDIDATE_ID!r}"
        raise ValueError(f"candidate ID must be {expectation}")
    return candidate_id


def render_tag(
    matrix: dict[str, Any],
    stream: dict[str, Any],
    arch: str | None = None,
) -> str:
    stream_tag = matrix["tag_policy"]["deploy_tag_template"].format(
        stream=stream["id"],
        release=stream["release"],
        distro=stream["distro"],
        base_tag=stream["base_tag"],
        tag_token=stream["tag_token"],
    )
    return f"{stream_tag}-{arch}" if arch else stream_tag


def render_candidate_tag(
    matrix: dict[str, Any],
    stream: dict[str, Any],
    candidate_id: str,
    arch: str | None = None,
) -> str:
    candidate_id = validate_candidate_id(candidate_id)
    template_name = (
        "candidate_arch_tag_template" if arch else "candidate_tag_template"
    )
    return matrix["tag_policy"][template_name].format(
        stream=stream["id"],
        release=stream["release"],
        distro=stream["distro"],
        base_tag=stream["base_tag"],
        tag_token=stream["tag_token"],
        candidate_id=candidate_id,
        arch=arch or "",
    )
```

In `scripts/validate-config.py`, make the exact policy and field sets:

```python
EXPECTED_TAG_POLICY = {
    "deploy_tag_template": "{release}-{distro}-{tag_token}",
    "candidate_tag_template": (
        "{release}-{distro}-{tag_token}-candidate-{candidate_id}"
    ),
    "candidate_arch_tag_template": (
        "{release}-{distro}-{tag_token}-candidate-{candidate_id}-{arch}"
    ),
}
DEPLOY_TEMPLATE_FIELDS = {"release", "distro", "tag_token"}
CANDIDATE_TEMPLATE_FIELDS = DEPLOY_TEMPLATE_FIELDS | {"candidate_id"}
CANDIDATE_ARCH_TEMPLATE_FIELDS = CANDIDATE_TEMPLATE_FIELDS | {"arch"}
```

Replace the current two-template block in `validate_matrix()` with:

```python
deploy_template = tag_policy.get("deploy_tag_template")
candidate_template = tag_policy.get("candidate_tag_template")
candidate_arch_template = tag_policy.get("candidate_arch_tag_template")
templates = {
    "deploy_tag_template": (
        deploy_template,
        DEPLOY_TEMPLATE_FIELDS,
    ),
    "candidate_tag_template": (
        candidate_template,
        CANDIDATE_TEMPLATE_FIELDS,
    ),
    "candidate_arch_tag_template": (
        candidate_arch_template,
        CANDIDATE_ARCH_TEMPLATE_FIELDS,
    ),
}
for name, (template, expected_fields) in templates.items():
    if not isinstance(template, str):
        errors.append(f"tag_policy.{name} must be a string")
        continue
    try:
        actual_fields = template_fields(template)
    except ValueError as error:
        errors.append(f"invalid tag template {name}: {error}")
        continue
    if actual_fields != expected_fields:
        errors.append(
            f"{name} fields must be exactly {sorted(expected_fields)!r}"
        )
if any(not isinstance(value[0], str) for value in templates.values()):
    return
if tag_policy != EXPECTED_TAG_POLICY:
    errors.append(f"tag_policy must be exactly {EXPECTED_TAG_POLICY!r}")

for stream in streams:
    if not isinstance(stream, dict) or stream.get("id") not in EXPECTED_STREAMS:
        continue
    stream_id = stream["id"]
    try:
        deploy_tag = deploy_template.format(**stream)
        if deploy_tag != stream_id:
            errors.append(
                f"deploy tag for stream {stream_id!r} must equal the stream ID"
            )
        candidate_tag = candidate_template.format(
            **stream,
            candidate_id="123456789-1",
        )
        if candidate_tag != f"{stream_id}-candidate-123456789-1":
            errors.append(f"candidate tag for stream {stream_id!r} is invalid")
        for arch in EXPECTED_ARCHITECTURES:
            candidate_arch_tag = candidate_arch_template.format(
                **stream,
                candidate_id="123456789-1",
                arch=arch,
            )
            expected = f"{stream_id}-candidate-123456789-1-{arch}"
            if candidate_arch_tag != expected:
                errors.append(
                    f"candidate architecture tag for {stream_id!r}/{arch!r} "
                    f"must be {expected!r}"
                )
    except (AttributeError, IndexError, KeyError, TypeError, ValueError) as error:
        errors.append(f"cannot render tags for stream {stream_id!r}: {error}")
```

- [x] **Step 4: Freeze candidate identity and separate candidate refs from stream aliases**

In `scripts/plan-publish.py`, import `LOCAL_DRY_RUN_CANDIDATE_ID`, `render_candidate_tag`, and `validate_candidate_id`. Add:

```python
parser.add_argument(
    "--candidate-id",
    default=LOCAL_DRY_RUN_CANDIDATE_ID,
    help=(
        "Workflow run candidate ID; local read-only plans default to "
        f"{LOCAL_DRY_RUN_CANDIDATE_ID}"
    ),
)
```

Use this exact plan construction rule:

```python
def build_plan(
    matrix: dict[str, Any],
    profile: dict[str, Any],
    stream: dict[str, Any],
    image_filter: str | None = None,
    candidate_id: str = LOCAL_DRY_RUN_CANDIDATE_ID,
) -> dict[str, Any]:
    candidate_id = validate_candidate_id(candidate_id)
    stream_tag = render_tag(matrix, stream)
    candidate_tag = render_candidate_tag(matrix, stream, candidate_id)
```

For each image, set the exact fields below and use the same candidate architecture tag in parent refs, leaf refs, and `kolla_build_push --tag`:

```python
arch_tag = render_candidate_tag(matrix, stream, candidate_id, arch)
arch_ref = image_ref(registry, owner, repository, image, arch_tag)

deploy_ref = image_ref(
    registry, owner, repository, image, candidate_tag
)
stream_ref = image_ref(
    registry, owner, repository, image, stream_tag
)
images.append(
    {
        "image": image,
        "kolla_ansible_variables": image_entry["kolla_ansible_variables"],
        "deploy_tag": candidate_tag,
        "deploy_ref": deploy_ref,
        "stream_ref": stream_ref,
        "expected_ghcr_ref": deploy_ref,
        "manifest_metadata_file": manifest_metadata_file(image, candidate_tag),
        "architectures": architectures,
        "commands": {
            "manifest_create": [
                "docker", "buildx", "imagetools", "create",
                "--tag", deploy_ref,
                "--metadata-file", manifest_metadata_file(image, candidate_tag),
                *arch_refs,
            ],
            "manifest_inspect": [
                "docker", "buildx", "imagetools", "inspect", deploy_ref
            ],
        },
    }
)
```

Add `"candidate_id": candidate_id` to the returned root and call:

```python
plan = build_plan(
    matrix,
    profile,
    stream,
    args.image,
    args.candidate_id,
)
```

- [x] **Step 5: Bind approval recomputation to a non-local trusted candidate ID**

In `scripts/validate-publish-approval.py`, require:

```python
from profile_resolver import validate_candidate_id

parser.add_argument("--expected-candidate-id", required=True)
```

Merge `validate_candidate_id` into the script's existing resolver import.

Change `planner_inputs()` and `render_expected_plan()` exactly as follows, then use the trusted-ID check before canonical plan comparison:

```python
def planner_inputs(plan: dict[str, Any]) -> tuple[str, str, str | None, str]:
    stream = required_string(plan, "stream")
    profile = required_string(plan, "profile")
    candidate_id = required_string(plan, "candidate_id")
    if "image_filter" not in plan:
        raise plan_mismatch("image_filter")
    image_filter = plan["image_filter"]
    if image_filter is not None and type(image_filter) is not str:
        raise plan_mismatch("image_filter")
    return stream, profile, image_filter, candidate_id


def render_expected_plan(
    stream: str,
    profile: str,
    image_filter: str | None,
    candidate_id: str,
) -> dict[str, Any]:
    command = [
        sys.executable,
        str(PLAN_PUBLISH),
        "--stream", stream,
        "--profile", profile,
        "--candidate-id", candidate_id,
        "--dry-run",
    ]
    if image_filter is not None:
        command.extend(["--image", image_filter])
    result = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or "planner failed without an error message"
        raise ValueError(f"Unable to recompute publish plan: {detail}")
    expected = json.loads(result.stdout)
    if not isinstance(expected, dict):
        raise ValueError("Recomputed publish plan root is not a JSON object")
    return expected


def recompute_requirement(
    plan: dict[str, Any],
    expected_candidate_id: str,
) -> ApprovalRequirement | None:
    expected_candidate_id = validate_candidate_id(
        expected_candidate_id,
        allow_local=False,
    )
    stream_id, profile_name, image_filter, candidate_id = planner_inputs(plan)
    if candidate_id != expected_candidate_id:
        raise ValueError(
            "Frozen publish plan candidate ID does not match trusted workflow context"
        )
    expected_plan = render_expected_plan(
        stream_id,
        profile_name,
        image_filter,
        candidate_id,
    )
```

Keep the architecture, `publish_enabled`, complete canonical JSON, scope-variable, and phrase checks unchanged. Call `recompute_requirement(plan, args.expected_candidate_id)` from `main()`.

- [x] **Step 6: Run Task 1 tests and the full suite GREEN**

Run:

```bash
python3 -m unittest \
  tests.test_config_validation \
  tests.test_profile_resolution \
  tests.test_plan_publish \
  tests.test_publish_approval -v
python3 scripts/validate-config.py
python3 -m unittest discover -s tests -v
```

Expected: each command exits `0`; both unittest commands end with `OK`; configuration prints `Configuration validation passed.`

- [x] **Step 7: Amend the single workspace commit**

```bash
git add config/build-matrix.json scripts/profile_resolver.py \
  scripts/validate-config.py scripts/plan-publish.py \
  scripts/validate-publish-approval.py tests/test_config_validation.py \
  tests/test_profile_resolution.py tests/test_plan_publish.py \
  tests/test_publish_approval.py \
  docs/superpowers/plans/2026-07-14-publish-hardening.md
git commit --amend --no-edit
test "$(git rev-list --count origin/main..HEAD)" -eq 1
```

Expected: amend succeeds and the commit-count assertion exits `0`.

### Task 2: Strict Current-Run Kolla Build Summary Proof

**Files:**

- Create: `tests/fixtures/kolla-build-summary-contract.json`
- Create: `tests/test_kolla_build_summary_validation.py`
- Create: `scripts/validate-kolla-build-summary.py`

**Interfaces:**

- Consumes: `--kolla-summary PATH --publish-plan PATH --arch {amd64,arm64}`.
- Produces: exit `0` and `Kolla build summary validation passed.` only when `built` is the exact planned parent/leaf union and no planned build is failed, skipped, unbuildable, or unmatched.
- Produces: exit `1` with a deterministic error list for a well-formed but invalid result; exit `2` for unreadable JSON, duplicate keys, or invalid frozen-plan selection.
- Preserves: standard-library-only runtime and the existing native evidence schema.

- [x] **Step 1: Record the pinned common Kolla summary contract and exact source**

Download each pinned wheel into a temporary directory for provenance inspection only, extract `KollaWorker.summary` with `ast.get_source_segment`, and require all three extracted UTF-8 strings to be byte-identical before creating the fixture. Store that exact 4,324-byte string, without adding a terminal newline or normalizing whitespace, as the JSON string value `summary_method_source`. Use `json.dumps(segment)` to produce the escaped value that is inserted with `apply_patch`; do not generate the repository file with shell redirection. Its SHA-256 must be exactly `02c656c628dc9f127ada22d993e0693feae6c94ee5f42c5d06e9a54fccd959f0`; the source begins with `def summary(self):` and ends with `        return results`. The wheel inspection is not a committed dependency.

Create `tests/fixtures/kolla-build-summary-contract.json` with the exact metadata below plus the `summary_method_source` string extracted above:

```json
{
  "schema_version": 1,
  "source_extraction": "ast.get_source_segment for KollaWorker.summary",
  "summary_method_sha256": "02c656c628dc9f127ada22d993e0693feae6c94ee5f42c5d06e9a54fccd959f0",
  "versions": {
    "20.4.0": {
      "distribution": "kolla==20.4.0",
      "source_path": "kolla/image/kolla_worker.py",
      "module_sha256": "6a035d50858519474d9b60bf7e502621603c151375ca1bbfc9d06abb7fdf658a",
      "summary_method_sha256": "02c656c628dc9f127ada22d993e0693feae6c94ee5f42c5d06e9a54fccd959f0"
    },
    "21.1.0": {
      "distribution": "kolla==21.1.0",
      "source_path": "kolla/image/kolla_worker.py",
      "module_sha256": "fbaac910754a33c79490d781f9c137953d40ef6ed1624cdd74661970c0d86721",
      "summary_method_sha256": "02c656c628dc9f127ada22d993e0693feae6c94ee5f42c5d06e9a54fccd959f0"
    },
    "22.0.0": {
      "distribution": "kolla==22.0.0",
      "source_path": "kolla/image/kolla_worker.py",
      "module_sha256": "a70c25776f2a10c73aa02fe90a9143fe269af1a1ca39bb2e6f989d737205ef9f",
      "summary_method_sha256": "02c656c628dc9f127ada22d993e0693feae6c94ee5f42c5d06e9a54fccd959f0"
    }
  },
  "top_level_keys": ["built", "failed", "not_matched", "skipped", "unbuildable"],
  "entry_keys": {
    "built": ["name"],
    "failed": ["name", "status"],
    "not_matched": ["name"],
    "skipped": ["name"],
    "unbuildable": ["name"]
  },
  "failed_status_values": ["connection_error", "error", "parent_error", "push_error"]
}
```

- [x] **Step 2: Write RED CLI and schema tests**

Create `tests/test_kolla_build_summary_validation.py` with helpers that render a candidate plan, select one architecture, and write a valid summary:

```python
ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = ROOT / "scripts" / "validate-kolla-build-summary.py"
PLANNER = ROOT / "scripts" / "plan-publish.py"
CONTRACT = ROOT / "tests" / "fixtures" / "kolla-build-summary-contract.json"
TEST_CANDIDATE_ID = "123456789-1"
EXPECTED_METHOD_SHA256 = (
    "02c656c628dc9f127ada22d993e0693fe"
    "ae6c94ee5f42c5d06e9a54fccd959f0"
)
EXPECTED_VERSION_PROVENANCE = {
    "20.4.0": {
        "distribution": "kolla==20.4.0",
        "source_path": "kolla/image/kolla_worker.py",
        "module_sha256": "6a035d50858519474d9b60bf7e502621603c151375ca1bbfc9d06abb7fdf658a",
        "summary_method_sha256": EXPECTED_METHOD_SHA256,
    },
    "21.1.0": {
        "distribution": "kolla==21.1.0",
        "source_path": "kolla/image/kolla_worker.py",
        "module_sha256": "fbaac910754a33c79490d781f9c137953d40ef6ed1624cdd74661970c0d86721",
        "summary_method_sha256": EXPECTED_METHOD_SHA256,
    },
    "22.0.0": {
        "distribution": "kolla==22.0.0",
        "source_path": "kolla/image/kolla_worker.py",
        "module_sha256": "a70c25776f2a10c73aa02fe90a9143fe269af1a1ca39bb2e6f989d737205ef9f",
        "summary_method_sha256": EXPECTED_METHOD_SHA256,
    },
}


def candidate_plan() -> dict:
    result = subprocess.run(
        [
            sys.executable,
            str(PLANNER),
            "--stream", "2025.1-rocky-9",
            "--profile", "core",
            "--image", "keystone",
            "--candidate-id", TEST_CANDIDATE_ID,
            "--dry-run",
        ],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )
    return json.loads(result.stdout)


def expected_names(plan: dict, arch: str = "amd64") -> set[str]:
    architecture = next(
        entry for entry in plan["build"]["architectures"]
        if entry["arch"] == arch
    )
    return {
        entry["image"]
        for key in ("parents", "images")
        for entry in architecture[key]
    }


def valid_summary(plan: dict, arch: str = "amd64") -> dict:
    return {
        "built": [{"name": name} for name in sorted(expected_names(plan, arch))],
        "failed": [],
        "not_matched": [{"name": "glance-api"}],
        "skipped": [],
        "unbuildable": [],
    }


def run_validator(
    plan: dict,
    summary: dict | None = None,
    *,
    arch: str = "amd64",
    raw_summary: str | None = None,
    write_summary: bool = True,
    rewrite_summary_command: bool = True,
) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        plan = copy.deepcopy(plan)
        plan_path = temp_path / "publish-plan.json"
        summary_path = temp_path / "kolla-summary.json"
        command_arch = arch if arch in {"amd64", "arm64"} else "amd64"
        architecture = next(
            entry for entry in plan["build"]["architectures"]
            if entry["arch"] == command_arch
        )
        if rewrite_summary_command:
            command = architecture["commands"]["kolla_build_push"]
            summary_index = command.index("--summary-json-file") + 1
            command[summary_index] = str(summary_path)
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        if write_summary:
            content = raw_summary if raw_summary is not None else json.dumps(summary)
            summary_path.write_text(content, encoding="utf-8")
        return subprocess.run(
            [
                sys.executable,
                str(VALIDATOR),
                "--kolla-summary", str(summary_path),
                "--publish-plan", str(plan_path),
                "--arch", arch,
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )
```

Add the following test class (with `copy`, `hashlib`, `tempfile`, and `load_matrix` imports):

```python
class KollaBuildSummaryValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = candidate_plan()

    def test_fixture_covers_matrix_pins_and_exact_schema(self) -> None:
        fixture = json.loads(CONTRACT.read_text(encoding="utf-8"))
        self.assertEqual(
            set(fixture),
            {
                "schema_version",
                "source_extraction",
                "summary_method_source",
                "summary_method_sha256",
                "versions",
                "top_level_keys",
                "entry_keys",
                "failed_status_values",
            },
        )
        self.assertEqual(fixture["schema_version"], 1)
        self.assertEqual(
            fixture["source_extraction"],
            "ast.get_source_segment for KollaWorker.summary",
        )
        matrix_versions = {
            stream["kolla_version"] for stream in load_matrix()["streams"]
        }
        self.assertEqual(set(fixture["versions"]), matrix_versions)
        self.assertEqual(fixture["versions"], EXPECTED_VERSION_PROVENANCE)
        source_text = fixture["summary_method_source"]
        self.assertIs(type(source_text), str)
        source = source_text.encode("utf-8")
        self.assertFalse(source.endswith(b"\n"))
        self.assertEqual(len(source), 4324)
        self.assertTrue(source_text.startswith("def summary(self):"))
        self.assertTrue(source_text.endswith("        return results"))
        self.assertEqual(hashlib.sha256(source).hexdigest(), EXPECTED_METHOD_SHA256)
        self.assertEqual(fixture["summary_method_sha256"], EXPECTED_METHOD_SHA256)
        self.assertEqual(
            fixture["top_level_keys"],
            ["built", "failed", "not_matched", "skipped", "unbuildable"],
        )
        self.assertEqual(
            fixture["entry_keys"],
            {
                "built": ["name"],
                "failed": ["name", "status"],
                "not_matched": ["name"],
                "skipped": ["name"],
                "unbuildable": ["name"],
            },
        )
        self.assertEqual(
            fixture["failed_status_values"],
            ["connection_error", "error", "parent_error", "push_error"],
        )

    def test_exact_summary_passes(self) -> None:
        result = run_validator(self.plan, valid_summary(self.plan))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Kolla build summary validation passed.", result.stdout)

    def test_exact_arm64_summary_passes(self) -> None:
        result = run_validator(
            self.plan,
            valid_summary(self.plan, "arm64"),
            arch="arm64",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Kolla build summary validation passed.", result.stdout)

    def test_built_must_equal_exact_planned_union(self) -> None:
        missing = valid_summary(self.plan)
        missing["built"].pop()
        extra = valid_summary(self.plan)
        extra["built"].append({"name": "unexpected-image"})
        for name, summary, message in (
            ("missing", missing, "built is missing planned image"),
            ("extra", extra, "built contains unexpected image: unexpected-image"),
        ):
            with self.subTest(case=name):
                result = run_validator(self.plan, summary)
                self.assertEqual(result.returncode, 1)
                self.assertIn(message, result.stderr)

    def test_failure_skip_and_unbuildable_buckets_must_be_empty(self) -> None:
        cases = []
        for status in ("connection_error", "error", "parent_error", "push_error"):
            summary = valid_summary(self.plan)
            summary["failed"] = [{"name": "other-image", "status": status}]
            cases.append((f"failed-{status}", summary, "failed must be empty"))
        for bucket in ("skipped", "unbuildable"):
            summary = valid_summary(self.plan)
            summary[bucket] = [{"name": "other-image"}]
            cases.append((bucket, summary, f"{bucket} must be empty"))
        for name, summary, message in cases:
            with self.subTest(case=name):
                result = run_validator(self.plan, summary)
                self.assertEqual(result.returncode, 1)
                self.assertIn(message, result.stderr)

    def test_planned_name_must_not_be_unmatched(self) -> None:
        summary = valid_summary(self.plan)
        summary["not_matched"].append({"name": "keystone"})
        result = run_validator(self.plan, summary)
        self.assertEqual(result.returncode, 1)
        self.assertIn("planned image appears in not_matched: keystone", result.stderr)

    def test_duplicate_and_cross_bucket_names_are_rejected(self) -> None:
        duplicate = valid_summary(self.plan)
        duplicate["built"].append(copy.deepcopy(duplicate["built"][0]))
        cross = valid_summary(self.plan)
        cross["not_matched"].append({"name": cross["built"][0]["name"]})
        for summary, message in (
            (duplicate, "built contains duplicate image"),
            (cross, "image appears in both built and not_matched"),
        ):
            result = run_validator(self.plan, summary)
            self.assertEqual(result.returncode, 1)
            self.assertIn(message, result.stderr)

    def test_root_bucket_and_entry_schemas_are_exact(self) -> None:
        cases = []
        missing = valid_summary(self.plan)
        missing.pop("skipped")
        cases.append((missing, "summary keys must be exactly"))
        unexpected = valid_summary(self.plan)
        unexpected["extra"] = []
        cases.append((unexpected, "summary keys must be exactly"))
        not_list = valid_summary(self.plan)
        not_list["skipped"] = {}
        cases.append((not_list, "skipped must be a list"))
        not_object = valid_summary(self.plan)
        not_object["built"][0] = "base"
        cases.append((not_object, "built[0] keys must be exactly"))
        extra_key = valid_summary(self.plan)
        extra_key["built"][0]["status"] = "error"
        cases.append((extra_key, "built[0] keys must be exactly"))
        invalid_name = valid_summary(self.plan)
        invalid_name["built"][0]["name"] = "Bad/Image"
        cases.append((invalid_name, "built[0].name is invalid"))
        invalid_status = valid_summary(self.plan)
        invalid_status["failed"] = [{"name": "other-image", "status": "unknown"}]
        cases.append((invalid_status, "failed[0].status is invalid"))
        for summary, message in cases:
            with self.subTest(message=message):
                result = run_validator(self.plan, summary)
                self.assertEqual(result.returncode, 1)
                self.assertIn(message, result.stderr)

    def test_duplicate_json_key_invalid_json_and_absent_file_exit_two(self) -> None:
        valid = json.dumps(valid_summary(self.plan))
        duplicate = valid.replace('{"built":', '{"built": [], "built":', 1)
        for raw, write_summary, message in (
            (duplicate, True, "duplicate JSON object key"),
            ("{", True, "Expecting"),
            (None, False, "No such file"),
        ):
            with self.subTest(message=message):
                result = run_validator(
                    self.plan,
                    raw_summary=raw,
                    write_summary=write_summary,
                )
                self.assertEqual(result.returncode, 2)
                self.assertIn(message, result.stderr)

    def test_wrong_architecture_is_rejected(self) -> None:
        result = run_validator(
            self.plan,
            valid_summary(self.plan),
            arch="ppc64le",
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("invalid choice", result.stderr)

    def test_malformed_architecture_commands_exit_two_without_traceback(self) -> None:
        for commands, message in (
            (None, "frozen plan amd64 commands must be an object"),
            ([], "frozen plan amd64 commands must be an object"),
            ("kolla-build", "frozen plan amd64 commands must be an object"),
            ({}, "frozen Kolla command must be a string argv list"),
        ):
            with self.subTest(commands=commands):
                plan = copy.deepcopy(self.plan)
                architecture = next(
                    entry for entry in plan["build"]["architectures"]
                    if entry["arch"] == "amd64"
                )
                architecture["commands"] = commands
                result = run_validator(
                    plan,
                    valid_summary(plan),
                    rewrite_summary_command=False,
                )
                self.assertEqual(result.returncode, 2)
                self.assertIn(message, result.stderr)
                self.assertNotIn("Traceback", result.stderr)

    def test_incomplete_current_summary_rejects_stale_remote_tag_scenario(self) -> None:
        summary = valid_summary(self.plan)
        summary["built"] = [
            entry for entry in summary["built"] if entry["name"] != "keystone"
        ]
        result = run_validator(self.plan, summary)
        self.assertEqual(result.returncode, 1)
        self.assertIn("built is missing planned image: keystone", result.stderr)
```

- [x] **Step 3: Run the new tests and capture RED evidence**

Run:

```bash
python3 -m unittest tests.test_kolla_build_summary_validation -v
```

Expected: tests fail because `scripts/validate-kolla-build-summary.py` does not exist. Save the failing result in `.context/publish-hardening-red-task2.txt`.

- [x] **Step 4: Implement the strict standard-library validator**

Create `scripts/validate-kolla-build-summary.py` with these exact constants and validation rules:

```python
#!/usr/bin/env python3
"""Validate the current Kolla JSON summary against one frozen native build."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

IMAGE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
BUCKETS = ("built", "failed", "not_matched", "skipped", "unbuildable")
ENTRY_KEYS = {
    "built": {"name"},
    "failed": {"name", "status"},
    "not_matched": {"name"},
    "skipped": {"name"},
    "unbuildable": {"name"},
}
FAILED_STATUSES = {"connection_error", "error", "parent_error", "push_error"}


def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as file_obj:
        return json.load(file_obj, object_pairs_hook=reject_duplicate_keys)


def planned_build(plan: dict[str, Any], arch: str) -> tuple[set[str], Path]:
    build = plan.get("build")
    if type(build) is not dict:
        raise ValueError("frozen plan build must be an object")
    architectures = build.get("architectures")
    if type(architectures) is not list:
        raise ValueError("frozen plan build architectures must be a list")
    matches = []
    for entry in architectures:
        if type(entry) is not dict:
            raise ValueError("frozen plan architecture entry must be an object")
        if entry.get("arch") == arch:
            matches.append(entry)
    if len(matches) != 1:
        raise ValueError(f"frozen plan must contain exactly one {arch} build")
    architecture = matches[0]
    names: list[str] = []
    for bucket in ("parents", "images"):
        entries = architecture.get(bucket)
        if type(entries) is not list:
            raise ValueError(f"frozen plan {arch} {bucket} must be a list")
        for entry in entries:
            if type(entry) is not dict or type(entry.get("image")) is not str:
                raise ValueError(f"frozen plan {arch} {bucket} entry is invalid")
            names.append(entry["image"])
    if len(names) != len(set(names)):
        raise ValueError(f"frozen plan {arch} build names must be unique")
    commands = architecture.get("commands")
    if type(commands) is not dict:
        raise ValueError(f"frozen plan {arch} commands must be an object")
    command = commands.get("kolla_build_push")
    if type(command) is not list or not all(type(part) is str for part in command):
        raise ValueError("frozen Kolla command must be a string argv list")
    positions = [index for index, part in enumerate(command) if part == "--summary-json-file"]
    if len(positions) != 1 or positions[0] + 1 >= len(command):
        raise ValueError("frozen Kolla command must contain one summary path")
    return set(names), Path(command[positions[0] + 1])


def validate_summary(summary: Any, expected: set[str]) -> list[str]:
    if type(summary) is not dict:
        return ["Kolla build summary must be an object"]
    errors: list[str] = []
    if set(summary) != set(BUCKETS):
        errors.append(f"Kolla build summary keys must be exactly {sorted(BUCKETS)!r}")
    names_by_bucket: dict[str, set[str]] = {}
    all_names: dict[str, str] = {}
    for bucket in BUCKETS:
        entries = summary.get(bucket)
        if type(entries) is not list:
            errors.append(f"{bucket} must be a list")
            names_by_bucket[bucket] = set()
            continue
        names: set[str] = set()
        for index, entry in enumerate(entries):
            if type(entry) is not dict or set(entry) != ENTRY_KEYS[bucket]:
                errors.append(
                    f"{bucket}[{index}] keys must be exactly {sorted(ENTRY_KEYS[bucket])!r}"
                )
                continue
            name = entry["name"]
            if type(name) is not str or not IMAGE_NAME_RE.fullmatch(name):
                errors.append(f"{bucket}[{index}].name is invalid")
                continue
            if bucket == "failed":
                status = entry["status"]
                if type(status) is not str or status not in FAILED_STATUSES:
                    errors.append(f"failed[{index}].status is invalid")
            if name in names:
                errors.append(f"{bucket} contains duplicate image: {name}")
            names.add(name)
            previous = all_names.get(name)
            if previous is not None and previous != bucket:
                errors.append(f"image appears in both {previous} and {bucket}: {name}")
            all_names[name] = bucket
        names_by_bucket[bucket] = names
    built = names_by_bucket.get("built", set())
    for name in sorted(expected - built):
        errors.append(f"built is missing planned image: {name}")
    for name in sorted(built - expected):
        errors.append(f"built contains unexpected image: {name}")
    for bucket in ("failed", "skipped", "unbuildable"):
        if names_by_bucket.get(bucket):
            errors.append(f"{bucket} must be empty")
    for name in sorted(expected & names_by_bucket.get("not_matched", set())):
        errors.append(f"planned image appears in not_matched: {name}")
    return errors


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--kolla-summary", required=True, type=Path)
    parser.add_argument("--publish-plan", required=True, type=Path)
    parser.add_argument("--arch", required=True, choices=("amd64", "arm64"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        plan = load_json(args.publish_plan)
        if type(plan) is not dict:
            raise ValueError("frozen publish plan must be an object")
        expected, planned_summary_path = planned_build(plan, args.arch)
        if args.kolla_summary != planned_summary_path:
            raise ValueError(
                "Kolla summary path does not match the frozen command: "
                f"{args.kolla_summary} != {planned_summary_path}"
            )
        summary = load_json(args.kolla_summary)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Invalid Kolla build summary input: {exc}", file=sys.stderr)
        return 2

    errors = validate_summary(summary, expected)
    if errors:
        print("Kolla build summary validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("Kolla build summary validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [x] **Step 5: Run focused and full suites GREEN**

Run:

```bash
python3 -m unittest tests.test_kolla_build_summary_validation -v
python3 -m unittest discover -s tests -v
```

Expected: both commands exit `0` and end with `OK`.

- [x] **Step 6: Amend the single workspace commit**

```bash
git add tests/fixtures/kolla-build-summary-contract.json \
  tests/test_kolla_build_summary_validation.py \
  scripts/validate-kolla-build-summary.py \
  docs/superpowers/plans/2026-07-14-publish-hardening.md
git commit --amend --no-edit
test "$(git rev-list --count origin/main..HEAD)" -eq 1
```

Expected: amend succeeds and the branch still has exactly one local commit over `origin/main`.

### Task 3: Dispatch Isolation, Workflow Supply Chain, Credentials, Trusted Candidate Flow, Native Docker, and Kolla Ordering

**Files:**

- Modify: `.github/workflows/publish.yml:63-566`
- Modify: `.github/workflows/validate.yml:12-42`
- Modify: `tests/test_publish_workflow.py:1-332,493-504`

**Interfaces:**

- Consumes: Task 1's `--candidate-id` planner option and `--expected-candidate-id` approval option.
- Consumes: Task 2's `validate-kolla-build-summary.py --kolla-summary PATH --publish-plan PATH --arch ARCH`.
- Produces: one isolated workflow run per manual or CI-issued `workflow_dispatch`, with every artifact name qualified by `<run_id>-<run_attempt>` so separate streams and full reruns cannot collide.
- Produces: a normalized local Unix `DOCKER_HOST` inherited by Kolla, Docker CLI, pulls, inspections, and smoke after server OS/architecture verification.
- Produces: a fresh job-scoped `DOCKER_CONFIG` for each package-writing job and an `always()` cleanup step that removes its normal-run GHCR credential file without breaking the Buildx action's post-job builder cleanup.
- Produces: exact Action allowlist pins and five checkout blocks with `persist-credentials: false`.
- Preserves: exactly five `workflow_dispatch` inputs, three non-dry-run job gates, two package-writing jobs, and the existing native evidence JSON shape.
- Removes: `workflow_call`; CI automation must dispatch this workflow as its own run and must not embed publication as a reusable workflow job.

- [x] **Step 1: Add RED dispatch isolation, exact Action allowlist, and checkout-policy tests**

Add to `tests/test_publish_workflow.py`:

```python
EXPECTED_ACTIONS = {
    "actions/checkout": ("9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0", "v7"),
    "actions/upload-artifact": ("043fb46d1a93c77aae656e7c1c64a875d1fc6a0a", "v7"),
    "actions/download-artifact": ("3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c", "v8"),
    "actions/setup-python": ("ece7cb06caefa5fff74198d8649806c4678c61a1", "v6"),
    "docker/setup-buildx-action": ("bb05f3f5519dd87d3ba754cc423b652a5edd6d2c", "v4"),
}
ACTION_RE = re.compile(
    r"(?m)^\s*uses:\s+([^@\s]+)@([0-9a-f]{40})\s+#\s+(v[0-9]+)\s*$"
)


def expected_action_use(repository: str) -> str:
    sha, release = EXPECTED_ACTIONS[repository]
    return f"uses: {repository}@{sha} # {release}"


def test_dispatch_is_the_only_trigger_and_has_exact_frozen_inputs(self) -> None:
    dispatch = yaml_block(self.publish, "  workflow_dispatch:")
    expected_inputs = {"stream", "profile", "image", "dry_run", "approval"}
    inputs = set(re.findall(r"^      ([a-z_]+):$", dispatch, re.MULTILINE))
    self.assertEqual(inputs, expected_inputs)
    self.assertIn("type: string", yaml_block(dispatch, "      stream:"))
    dry_run = yaml_block(dispatch, "      dry_run:")
    self.assertIn("type: boolean", dry_run)
    self.assertIn("default: true", dry_run)
    self.assertNotIn("workflow_call:", self.publish)
    for legacy in ("release", "distro", "distro_version", "candidate_id"):
        self.assertNotIn(f"      {legacy}:", dispatch)


def test_workflows_use_only_reviewed_action_commits(self) -> None:
    combined = self.publish + "\n" + self.validate
    raw_uses = re.findall(r"(?m)^\s*uses:\s+.+$", combined)
    matches = ACTION_RE.findall(combined)
    self.assertEqual(len(raw_uses), 17)
    self.assertEqual(len(matches), len(raw_uses))
    for repository, sha, release in matches:
        with self.subTest(repository=repository):
            self.assertIn(repository, EXPECTED_ACTIONS)
            self.assertEqual((sha, release), EXPECTED_ACTIONS[repository])


def test_every_checkout_disables_persisted_credentials(self) -> None:
    checkout_header = (
        "uses: actions/checkout@"
        "9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0 # v7"
    )
    self.assertEqual(self.publish.count(checkout_header), 4)
    self.assertEqual(self.validate.count(checkout_header), 1)
    for document, count in ((self.publish, 4), (self.validate, 1)):
        blocks = re.findall(
            rf"(?ms)^\s*- name: Check out repository\n"
            rf"\s+{re.escape(checkout_header)}\n"
            rf"\s+with:\n\s+persist-credentials: false(?:\n|$)",
            document,
        )
        self.assertEqual(len(blocks), count)
```

Replace the old dual-trigger test with the dispatch-only test above. Rename the
cross-repository reusable-caller guard test to describe a repository-owned
dispatch while keeping the pre-checkout repository guard. Replace every
existing direct `actions/*@vN` or
`docker/setup-buildx-action@vN` assertion in this test module with
`expected_action_use("actions/checkout")`,
`expected_action_use("actions/upload-artifact")`,
`expected_action_use("actions/download-artifact")`,
`expected_action_use("actions/setup-python")`, or
`expected_action_use("docker/setup-buildx-action")` as appropriate. The
existing ordering and per-job count assertions remain unchanged.

- [x] **Step 2: Add RED tests for candidate provenance, Docker trust, and Kolla-summary ordering**

Add:

```python
def test_workflow_candidate_id_comes_only_from_run_context(self) -> None:
    expected = "CANDIDATE_ID: ${{ github.run_id }}-${{ github.run_attempt }}"
    self.assertEqual(self.publish.count(expected), 4)
    self.assertEqual(self.publish.count('--candidate-id "$CANDIDATE_ID"'), 1)
    self.assertEqual(
        self.publish.count('--expected-candidate-id "$CANDIDATE_ID"'),
        3,
    )
    dispatch = yaml_block(self.publish, "  workflow_dispatch:")
    self.assertNotIn("candidate_id:", dispatch)
    self.assertNotIn("workflow_call:", self.publish)


def test_every_artifact_name_is_candidate_qualified(self) -> None:
    candidate = "${{ github.run_id }}-${{ github.run_attempt }}"
    self.assertEqual(self.publish.count(f"name: publish-plan-{candidate}"), 4)
    self.assertEqual(
        self.publish.count(f"name: native-${{{{ matrix.arch }}}}-{candidate}"),
        1,
    )
    self.assertEqual(
        self.publish.count(
            f"name: native-diagnostics-${{{{ matrix.arch }}}}-{candidate}"
        ),
        1,
    )
    self.assertEqual(self.publish.count(f"name: native-amd64-{candidate}"), 1)
    self.assertEqual(self.publish.count(f"name: native-arm64-{candidate}"), 1)
    self.assertEqual(
        self.publish.count(
            f"name: publish-${{{{ inputs.stream }}}}-{candidate}"
        ),
        1,
    )
    for line in re.findall(r"(?m)^\s+name: (?:publish-plan|native-|publish-).+$", self.publish):
        self.assertIn(candidate, line)
    self.assertNotIn("overwrite:", self.publish)


def test_package_jobs_use_fresh_ephemeral_docker_config_and_always_cleanup(self) -> None:
    for name, suffix in (("build-native", "native"), ("finalize-publish", "finalize")):
        with self.subTest(job=name):
            job = self.publish_job(name)
            prepare = job.index("Prepare ephemeral Docker client state")
            buildx = job.index("Set up Docker Buildx")
            login = job.index("docker login ghcr.io")
            cleanup = job.index("Remove ephemeral Docker client state")
            self.assertLess(prepare, buildx)
            self.assertLess(buildx, login)
            self.assertLess(login, cleanup)
            cleanup_block = yaml_block(
                job,
                "      - name: Remove ephemeral Docker client state",
            )
            self.assertIn('rm -f -- "$DOCKER_CONFIG/config.json"', cleanup_block)
            self.assertNotIn("rm -rf", cleanup_block)
            preceding_step = (
                "Upload native diagnostics"
                if name == "build-native"
                else "Update convenience stream aliases"
            )
            self.assertLess(job.index(preceding_step), cleanup)
            for token in (
                "RUNNER_TEMP",
                "GITHUB_RUN_ID",
                "GITHUB_RUN_ATTEMPT",
                "DOCKER_CONFIG",
                "install -d -m 0700",
                "if: ${{ always() }}",
                'rm -f -- "$DOCKER_CONFIG/config.json"',
            ):
                self.assertIn(token, job)
            self.assertIn(suffix, job)
    self.assertEqual(self.publish.count("Prepare ephemeral Docker client state"), 2)
    self.assertEqual(self.publish.count("Remove ephemeral Docker client state"), 2)


def test_native_docker_is_local_linux_and_native_before_login(self) -> None:
    job = self.publish_job("build-native")
    buildx = job.index("Set up Docker Buildx")
    daemon = job.index("Validate native local Docker daemon")
    storage = job.index("Check Docker storage capacity")
    login = job.index("docker login ghcr.io")
    self.assertLess(buildx, daemon)
    self.assertLess(daemon, storage)
    self.assertLess(storage, login)
    for token in (
        "{{.OSType}}",
        "{{.Architecture}}",
        "EXPECTED_DOCKER_ARCH",
        "DOCKER_CONTEXT",
        "DOCKER_HOST",
        "docker context inspect",
        "unix:///",
        'printf \'DOCKER_HOST=%s\\n\' "$endpoint" >> "$GITHUB_ENV"',
        'printf \'DOCKER_CONTEXT=\\n\' >> "$GITHUB_ENV"',
    ):
        self.assertIn(token, job)


def test_current_kolla_summary_is_required_before_remote_inspection(self) -> None:
    job = self.publish_job("build-native")
    unlink = job.index("summary_path.unlink(missing_ok=True)")
    build = job.index("subprocess.run(command, check=True)")
    validate = job.index("scripts/validate-kolla-build-summary.py")
    remote = job.index("def inspect_remote_descriptor")
    self.assertLess(unlink, build)
    self.assertLess(build, validate)
    self.assertLess(validate, remote)
    self.assertIn('"--publish-plan", str(PLAN_PATH)', job)
    self.assertIn('"--arch", arch_name', job)


def test_kolla_version_command_substitution_is_not_duplicated(self) -> None:
    job = self.publish_job("build-native")
    self.assertEqual(job.count('KOLLA_VERSION="$('), 1)
```

In the existing authorization and pre-login tests, replace the old one-line
validator string with these two exact tokens and require both in each of the
three live jobs:

```python
approval_validator = "python3 scripts/validate-publish-approval.py"
candidate_binding = '--expected-candidate-id "$CANDIDATE_ID"'
for name in ("authorize-publish", "build-native", "finalize-publish"):
    job = self.publish_job(name)
    self.assertIn(approval_validator, job)
    self.assertIn(candidate_binding, job)
self.assertEqual(self.publish.count(approval_validator), 3)
self.assertEqual(self.publish.count(candidate_binding), 3)
```

- [x] **Step 3: Run workflow tests and capture RED evidence**

Run:

```bash
python3 -m unittest tests.test_publish_workflow -v
```

Expected: failures identify the reusable trigger, mutable Action majors, missing checkout policy, missing candidate context arguments, missing attempt-qualified artifacts, missing ephemeral Docker state, the duplicated Kolla assignment, missing Docker validation, and missing Kolla-summary ordering. Save the result in `.context/publish-hardening-red-task3.txt`.

- [x] **Step 4: Remove the reusable trigger, then pin all 17 Action uses and disable checkout credential persistence**

Delete the complete `workflow_call` block. Keep only the five existing
`workflow_dispatch` inputs. A same-repository or external CI pipeline invokes
the Actions workflow-dispatch API (or `gh workflow run`) so every requested
publish receives its own GitHub run ID, run artifact namespace, environment
review, and concurrency evaluation. Keep the existing repository-identity
guard and change its message from reusable-call wording to dispatch wording.
Do not add a caller-controlled candidate or artifact suffix.

Reconfirm the lightweight major tags from their official repositories:

```bash
git ls-remote https://github.com/actions/checkout.git 'refs/tags/v7' 'refs/tags/v7^{}'
git ls-remote https://github.com/actions/upload-artifact.git 'refs/tags/v7' 'refs/tags/v7^{}'
git ls-remote https://github.com/actions/download-artifact.git 'refs/tags/v8' 'refs/tags/v8^{}'
git ls-remote https://github.com/actions/setup-python.git 'refs/tags/v6' 'refs/tags/v6^{}'
git ls-remote https://github.com/docker/setup-buildx-action.git 'refs/tags/v4' 'refs/tags/v4^{}'
```

Expected commits in order are
`9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0`,
`043fb46d1a93c77aae656e7c1c64a875d1fc6a0a`,
`3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c`,
`ece7cb06caefa5fff74198d8649806c4678c61a1`, and
`bb05f3f5519dd87d3ba754cc423b652a5edd6d2c`; no peeled `^{}` line is expected
for these lightweight tags.

Use these exact references everywhere in both workflows:

```yaml
uses: actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0 # v7
uses: actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a # v7
uses: actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c # v8
uses: actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1 # v6
uses: docker/setup-buildx-action@bb05f3f5519dd87d3ba754cc423b652a5edd6d2c # v4
```

Every checkout block must be:

```yaml
- name: Check out repository
  uses: actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0 # v7
  with:
    persist-credentials: false
```

While editing the build job, remove the duplicated opening
`KOLLA_VERSION="$(` line so the command substitution has exactly one opener.

- [x] **Step 5: Pass trusted candidate identity through planning, artifacts, and all approval checks**

In the plan step, add the trusted environment value and exact planner argument:

```yaml
env:
  STREAM: ${{ inputs.stream }}
  PROFILE: ${{ inputs.profile }}
  IMAGE: ${{ inputs.image }}
  CANDIDATE_ID: ${{ github.run_id }}-${{ github.run_attempt }}
run: |
  set -euo pipefail
  mkdir -p artifacts/plan
  plan_args=(
    --stream "$STREAM"
    --profile "$PROFILE"
    --candidate-id "$CANDIDATE_ID"
    --dry-run
  )
```

In `authorize-publish`, `build-native`, and `finalize-publish`, add:

```yaml
CANDIDATE_ID: ${{ github.run_id }}-${{ github.run_attempt }}
```

and invoke:

```yaml
run: >-
  python3 scripts/validate-publish-approval.py
  --publish-plan artifacts/plan/publish-plan.json
  --expected-candidate-id "$CANDIDATE_ID"
```

Use the same trusted expression in every artifact upload/download name:

```yaml
name: publish-plan-${{ github.run_id }}-${{ github.run_attempt }}
name: native-${{ matrix.arch }}-${{ github.run_id }}-${{ github.run_attempt }}
name: native-diagnostics-${{ matrix.arch }}-${{ github.run_id }}-${{ github.run_attempt }}
name: native-amd64-${{ github.run_id }}-${{ github.run_attempt }}
name: native-arm64-${{ github.run_id }}-${{ github.run_attempt }}
name: publish-${{ inputs.stream }}-${{ github.run_id }}-${{ github.run_attempt }}
```

The first name appears once on upload and three times on download. Matrix
uploads use the matrix expression, while the finalizer downloads the two
literal architecture names. Do not enable artifact overwrite. A full workflow
rerun creates a new attempt-qualified candidate and complete artifact set. The
supported recovery procedure is **Re-run all jobs**. A partial rerun in which
an upstream producer is not rerun cannot find that attempt's artifact or
satisfy candidate binding and therefore fails closed. If GitHub reruns a
failed producer and all of its dependents, it may form a coherent chain, but
that path is not the documented operator procedure.

- [x] **Step 6: Isolate Docker client credentials in both package-writing jobs**

In `build-native`, immediately before Buildx, add:

```yaml
- name: Prepare ephemeral Docker client state
  env:
    DOCKER_CONFIG_SUFFIX: native-${{ matrix.arch }}
  run: |
    set -euo pipefail
    docker_config="$RUNNER_TEMP/kolla-docker-$GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT-$DOCKER_CONFIG_SUFFIX"
    case "$docker_config" in
      "$RUNNER_TEMP"/kolla-docker-*) ;;
      *) echo "Refusing unsafe Docker config path: $docker_config" >&2; exit 1 ;;
    esac
    rm -rf -- "$docker_config"
    install -d -m 0700 "$docker_config"
    printf 'DOCKER_CONFIG=%s\n' "$docker_config" >> "$GITHUB_ENV"
```

Add the same step before Buildx in `finalize-publish`, with
`DOCKER_CONFIG_SUFFIX: finalize`. Add this as the final ordinary step of each
package job, after native diagnostics in `build-native` and after alias
handling in `finalize-publish`:

```yaml
- name: Remove ephemeral Docker client state
  if: ${{ always() }}
  run: |
    set -euo pipefail
    if [ -z "${DOCKER_CONFIG:-}" ]; then
      exit 0
    fi
    case "$DOCKER_CONFIG" in
      "$RUNNER_TEMP"/kolla-docker-*) ;;
      *) echo "Refusing unsafe Docker config cleanup: $DOCKER_CONFIG" >&2; exit 1 ;;
    esac
    rm -f -- "$DOCKER_CONFIG/config.json"
```

Do not remove the whole directory in this final step: the pinned Buildx action
has a post-job hook that still needs its state to remove the builder. Removing
only `config.json` clears the Docker login credentials, then the action post
and runner temp cleanup dispose of non-secret builder/client state. Keep the
operational runner cleanup/reimaging requirement because a forced runner
termination can prevent both cleanup paths from executing.

- [x] **Step 7: Validate and normalize the local native Docker endpoint after Buildx**

Move `Check Docker storage capacity` after this new step and before login:

```yaml
- name: Validate native local Docker daemon
  env:
    EXPECTED_DOCKER_ARCH: ${{ matrix.runner_machine }}
  run: |
    set -euo pipefail
    if [ -n "${DOCKER_CONTEXT:-}" ]; then
      endpoint="$(docker context inspect "$DOCKER_CONTEXT" --format '{{.Endpoints.docker.Host}}')"
    elif [ -n "${DOCKER_HOST:-}" ]; then
      endpoint="$DOCKER_HOST"
    else
      endpoint="$(docker context inspect --format '{{.Endpoints.docker.Host}}')"
    fi
    if [[ "$endpoint" != unix:///* ]]; then
      echo "Docker endpoint must be a local Unix socket, got $endpoint" >&2
      exit 1
    fi
    server_os="$(docker info --format '{{.OSType}}')"
    server_arch="$(docker info --format '{{.Architecture}}')"
    if [ "$server_os" != linux ]; then
      echo "Docker server OS must be linux, got $server_os" >&2
      exit 1
    fi
    if [ "$server_arch" != "$EXPECTED_DOCKER_ARCH" ]; then
      echo "Docker server architecture must be $EXPECTED_DOCKER_ARCH, got $server_arch" >&2
      exit 1
    fi
    printf 'DOCKER_HOST=%s\n' "$endpoint" >> "$GITHUB_ENV"
    printf 'DOCKER_CONTEXT=\n' >> "$GITHUB_ENV"
    echo "Native local Docker daemon verified: $server_os/$server_arch $endpoint"
```

This normalizes later Docker CLI and Docker SDK/Kolla calls to the same verified Unix endpoint.

- [x] **Step 8: Delete and validate the exact current Kolla summary before remote inspection**

Add `sys` to the inline Python imports. Immediately before Kolla invocation, derive and check the exact planned summary path:

```python
summary_positions = [
    index for index, part in enumerate(command) if part == "--summary-json-file"
]
if len(summary_positions) != 1 or summary_positions[0] + 1 >= len(command):
    raise SystemExit("planned Kolla command must contain one summary path")
summary_path = pathlib.Path(command[summary_positions[0] + 1])
expected_summary_path = pathlib.Path(
    f"artifacts/kolla-summary/{plan['stream']}-{arch_name}.json"
)
if summary_path != expected_summary_path:
    raise SystemExit("planned Kolla summary path is invalid")
summary_path.unlink(missing_ok=True)
subprocess.run(command, check=True)
subprocess.run(
    [
        sys.executable,
        "scripts/validate-kolla-build-summary.py",
        "--kolla-summary", str(summary_path),
        "--publish-plan", str(PLAN_PATH),
        "--arch", arch_name,
    ],
    check=True,
)
```

Keep `inspect_remote_descriptor()`, immutable pulls, native smoke, and evidence writing after this block without changing the evidence schema.

- [x] **Step 9: Run workflow/config/full verification GREEN**

Run:

```bash
python3 -m unittest tests.test_publish_workflow -v
python3 scripts/validate-config.py
python3 -m unittest discover -s tests -v
actionlint .github/workflows/*.yml
```

Expected: unittest commands end with `OK`, configuration validation passes, and `actionlint` emits no diagnostics.

- [x] **Step 10: Amend the single workspace commit**

```bash
git add .github/workflows/publish.yml .github/workflows/validate.yml \
  tests/test_publish_workflow.py \
  docs/superpowers/plans/2026-07-14-publish-hardening.md
git commit --amend --no-edit
test "$(git rev-list --count origin/main..HEAD)" -eq 1
```

Expected: amend succeeds and the one-commit constraint remains true.

### Task 4: Candidate-Bound Publish Summary and Lock, Artifact-First Alias Update

**Files:**

- Modify: `scripts/validate-publish-summary.py:1-337`
- Modify: `scripts/generate-lock.py:43-152`
- Modify: `.github/workflows/publish.yml:568-972`
- Modify: `tests/test_publish_summary_validation.py:1-650`
- Modify: `tests/test_lock_generation.py:1-660`
- Modify: `tests/test_publish_workflow.py:321-492`

**Interfaces:**

- Consumes: `validate_publish_summary(matrix, profile, stream, summary, allow_partial, image_filter, candidate_id) -> list[str]`.
- Consumes: `validate-publish-summary.py --publish-summary PATH --stream STREAM --profile PROFILE --candidate-id ID [--allow-partial --image IMAGE]` and `generate-lock.py --publish-summary PATH --stream STREAM --profile deployment --candidate-id ID [--output PATH]`.
- Produces: a publish summary with exact top-level `candidate_id`, candidate `deploy_tag`/`deploy_ref`, and candidate child refs.
- Produces: the existing generic candidate-lock schema whose root variables and metadata `deploy_ref` use the candidate tag and whose `immutable_ref` remains `repository@sha256:<digest>`.
- Produces: a post-upload sequential alias step that writes each stable `stream_ref` from the validated candidate's immutable digest.
- Preserves: candidate-lock eligibility only for exact `deployment/all`; no environment state or promotion behavior.

- [x] **Step 1: Write RED candidate-summary and candidate-lock tests**

In both summary/lock test modules, define `TEST_CANDIDATE_ID = "123456789-1"`, add top-level `candidate_id`, construct refs with `render_candidate_tag()`, and make their subprocess helpers pass the expected ID exactly as shown:

```python
from scripts.profile_resolver import (
    find_stream,
    load_matrix,
    load_profile,
    render_candidate_tag,
    resolve_profile,
)

candidate_tag = "2025.1-rocky-9-candidate-123456789-1"
candidate_ref = (
    "ghcr.io/supergate-hub/kolla-container-images/keystone:"
    + candidate_tag
)
amd64_ref = candidate_ref + "-amd64"
arm64_ref = candidate_ref + "-arm64"
stream_ref = (
    "ghcr.io/supergate-hub/kolla-container-images/keystone:2025.1-rocky-9"
)


def run_validator_json(
    summary_json: str,
    *,
    stream: str = "2025.1-rocky-9",
    profile: str = "deployment",
    candidate_id: str = TEST_CANDIDATE_ID,
    allow_partial: bool = False,
    image: str | None = None,
) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory() as temp_dir:
        summary_path = Path(temp_dir) / "publish-summary.json"
        summary_path.write_text(summary_json, encoding="utf-8")
        command = [
            sys.executable,
            str(VALIDATE_SUMMARY),
            "--publish-summary", str(summary_path),
            "--stream", stream,
            "--profile", profile,
            "--candidate-id", candidate_id,
        ]
        if allow_partial:
            command.append("--allow-partial")
        if image is not None:
            command.extend(["--image", image])
        return subprocess.run(
            command,
            cwd=ROOT,
            text=True,
            capture_output=True,
        )


def run_validator(
    summary: dict,
    *,
    stream: str = "2025.1-rocky-9",
    profile: str = "deployment",
    candidate_id: str = TEST_CANDIDATE_ID,
    allow_partial: bool = False,
    image: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return run_validator_json(
        json.dumps(summary),
        stream=stream,
        profile=profile,
        candidate_id=candidate_id,
        allow_partial=allow_partial,
        image=image,
    )
```

Add these exact assertions:

```python
def test_stable_stream_ref_is_rejected_as_candidate_deploy_ref(self) -> None:
    summary = publish_summary()
    entry = image_entry(summary, "keystone")
    entry["deploy_tag"] = "2025.1-rocky-9"
    entry["deploy_ref"] = stream_ref
    result = run_validator(summary)
    self.assertEqual(result.returncode, 1)
    self.assertIn("candidate-123456789-1", result.stderr)

def test_candidate_lock_root_and_metadata_use_candidate_ref(self) -> None:
    summary = publish_summary()
    result, lock = generate_lock(summary)
    self.assertEqual(result.returncode, 0, result.stderr)
    parsed = parse_lock_yaml(lock)
    entry = parsed["_kolla_candidate_lock"]["images"]["keystone"]
    self.assertEqual(entry["deploy_ref"], candidate_ref)
    self.assertEqual(parsed["keystone_image_full"], candidate_ref)
    self.assertEqual(
        entry["immutable_ref"],
        "ghcr.io/supergate-hub/kolla-container-images/keystone@"
        + entry["manifest_digest"],
    )


def test_summary_candidate_id_must_match_expected_id(self) -> None:
    summary = publish_summary()
    summary["candidate_id"] = "123456789-2"
    result = run_validator(summary)
    self.assertEqual(result.returncode, 1)
    self.assertIn("candidate_id must be '123456789-1'", result.stderr)


def test_malformed_expected_candidate_id_is_rejected(self) -> None:
    result = run_validator(publish_summary(), candidate_id="01-1")
    self.assertEqual(result.returncode, 2)
    self.assertIn("candidate ID", result.stderr)


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
```

Update `generate_lock_json()` and `generate_lock()` with a
`candidate_id: str = TEST_CANDIDATE_ID` keyword and add this exact command
pair before `--output`:

```python
"--candidate-id",
candidate_id,
```

The wrapper forwards it exactly:

```python
def generate_lock(
    summary: dict,
    *,
    stream: str = "2025.1-rocky-9",
    profile: str = "deployment",
    candidate_id: str = TEST_CANDIDATE_ID,
) -> tuple[subprocess.CompletedProcess[str], str | None]:
    return generate_lock_json(
        json.dumps(summary),
        stream=stream,
        profile=profile,
        candidate_id=candidate_id,
    )
```

Replace each module's `summary_image()` tag construction and publish-summary
root with:

```python
def summary_image(stream: dict, profile_image: dict, index: int) -> dict:
    image = profile_image["name"]
    deploy_tag = render_candidate_tag(MATRIX, stream, TEST_CANDIDATE_ID)
    repository = (
        f"{MATRIX['registry']}/{MATRIX['owner']}/{MATRIX['repository']}/{image}"
    )
    return {
        "image": image,
        "kolla_ansible_variables": profile_image["kolla_ansible_variables"],
        "deploy_tag": deploy_tag,
        "deploy_ref": f"{repository}:{deploy_tag}",
        "manifest_digest": digest(index * 10 + 9),
        "architectures": [
            {
                "arch": arch,
                "platform": f"linux/{arch}",
                "arch_ref": (
                    f"{repository}:"
                    f"{render_candidate_tag(MATRIX, stream, TEST_CANDIDATE_ID, arch)}"
                ),
                "digest": digest(index * 10 + arch_index + 1),
            }
            for arch_index, arch in enumerate(MATRIX["architectures"])
        ],
    }


def publish_summary(
    stream_id: str = "2025.1-rocky-9",
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
        "candidate_id": TEST_CANDIDATE_ID,
        "stream": stream["id"],
        "release": stream["release"],
        "distro": stream["distro"],
        "distro_version": stream["base_tag"],
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
```

- [x] **Step 2: Add RED finalization-order and alias-source tests**

Add to `tests/test_publish_workflow.py`:

```python
def test_candidate_artifact_is_uploaded_before_stream_alias_updates(self) -> None:
    job = self.publish_job("finalize-publish")
    manifests = job.index("Create and verify candidate multi-architecture manifests")
    validate = job.index("Validate summary and generate eligible candidate lock")
    upload = job.index("Upload publish artifacts")
    aliases = job.index("Update convenience stream aliases")
    self.assertLess(manifests, validate)
    self.assertLess(validate, upload)
    self.assertLess(upload, aliases)

def test_stream_aliases_are_created_from_candidate_immutable_digests(self) -> None:
    step = yaml_block(
        self.publish_job("finalize-publish"),
        "      - name: Update convenience stream aliases",
    )
    self.assertIn('stream_ref = planned_image["stream_ref"]', step)
    self.assertIn('candidate_ref = summary_image["deploy_ref"]', step)
    self.assertIn('immutable_ref = f"{repository}@{manifest_digest}"', step)
    self.assertRegex(
        step,
        r'"imagetools",\s+"create",\s+"--tag",\s+stream_ref,\s+immutable_ref',
    )
    self.assertIn("if stream_raw.stdout != immutable_raw.stdout:", step)
```

- [x] **Step 3: Run summary, lock, and workflow tests and capture RED evidence**

Run:

```bash
python3 -m unittest \
  tests.test_publish_summary_validation \
  tests.test_lock_generation \
  tests.test_publish_workflow -v
```

Expected: failures show stable-ref expectations, missing summary `candidate_id`, missing CLI candidate binding, and absent artifact-first alias step. Save the result in `.context/publish-hardening-red-task4.txt`.

- [x] **Step 4: Make publish-summary validation candidate-aware**

In `scripts/validate-publish-summary.py`:

```python
from profile_resolver import (
    find_stream,
    load_matrix,
    load_profile,
    render_candidate_tag,
    resolve_profile,
    validate_candidate_id,
)

SUMMARY_KEYS = frozenset(
    {
        "candidate_id",
        "stream",
        "release",
        "distro",
        "distro_version",
        "profile",
        "scope",
        "registry",
        "owner",
        "repository",
        "images",
    }
)
```

Require `--candidate-id` and change the relevant signatures exactly as follows:

```python
def validate_scope(
    summary: dict[str, Any],
    matrix: dict[str, Any],
    profile: dict[str, Any],
    stream: dict[str, Any],
    image_filter: str | None,
    image_count: int,
    candidate_id: str,
) -> list[str]:
    expected_identity = {
        "candidate_id": candidate_id,
        "stream": stream["id"],
        "release": stream["release"],
        "distro": stream["distro"],
        "distro_version": stream["base_tag"],
        "profile": profile["name"],
        "registry": matrix["registry"],
        "owner": matrix["owner"],
        "repository": matrix["repository"],
    }
    errors: list[str] = []
    for key, expected_value in expected_identity.items():
        actual = summary.get(key)
        if type(actual) is not type(expected_value) or actual != expected_value:
            errors.append(
                f"publish summary {key} must be {expected_value!r}, got {actual!r}"
            )
    expected_scope = {
        "profile": profile["name"],
        "image": image_filter or "all",
        "image_count": image_count,
    }
    if not exact_mapping(summary.get("scope"), expected_scope):
        errors.append(
            f"publish summary scope must be {expected_scope!r}, "
            f"got {summary.get('scope')!r}"
        )
    return errors


def validate_image(
    image: str,
    expected_profile_image: dict[str, Any],
    image_summary: dict[str, Any],
    matrix: dict[str, Any],
    stream: dict[str, Any],
    candidate_id: str,
) -> list[str]:
    errors: list[str] = []
    deploy_tag = render_candidate_tag(matrix, stream, candidate_id)
    expected_ref = image_ref(
        matrix["registry"],
        matrix["owner"],
        matrix["repository"],
        image,
        deploy_tag,
    )

    # Inside the existing `for arch in expected_arches` loop:
    expected_arch_ref = image_ref(
        matrix["registry"],
        matrix["owner"],
        matrix["repository"],
        image,
        render_candidate_tag(matrix, stream, candidate_id, arch),
    )


def validate_publish_summary(
    matrix: dict[str, Any],
    profile: dict[str, Any],
    stream: dict[str, Any],
    summary: dict[str, Any],
    allow_partial: bool,
    image_filter: str | None,
    candidate_id: str,
) -> list[str]:
    candidate_id = validate_candidate_id(candidate_id)
```

Pass `candidate_id` from `validate_publish_summary()` to `validate_scope()` and
`validate_image()`. In `parse_args()` add:

```python
parser.add_argument("--candidate-id", required=True)
```

In `main()`, call:

```python
errors = validate_publish_summary(
    matrix,
    profile,
    stream,
    summary,
    args.allow_partial,
    args.image,
    args.candidate_id,
)
```

Keep exact summary keys, profile variables, digest syntax, architecture set, and partial-scope policy unchanged.

- [x] **Step 5: Bind lock generation to the expected candidate without changing eligibility**

In `scripts/generate-lock.py`, add:

```python
from profile_resolver import (
    find_stream,
    load_matrix,
    load_profile,
    resolve_profile,
    validate_candidate_id,
)

parser.add_argument("--candidate-id", required=True)
```

Change the renderer signature and validation block to:

```python
def render_lock(
    matrix: dict[str, Any],
    profile: dict[str, Any],
    stream: dict[str, Any],
    summary: dict[str, Any],
    candidate_id: str,
) -> str:
    candidate_id = validate_candidate_id(candidate_id)
    if summary.get("candidate_id") != candidate_id:
        raise ValueError(
            "publish summary candidate ID does not match expected candidate ID"
        )
    errors = VALIDATE_PUBLISH_SUMMARY(
        matrix,
        profile,
        stream,
        summary,
        False,
        None,
        candidate_id,
    )
```

In `main()`, call:

```python
lock_yaml = render_lock(
    matrix,
    profile,
    stream,
    summary,
    args.candidate_id,
)
```

Keep root variables equal to `image_summary["deploy_ref"]`; because the validated summary is candidate-bound, no renderer-side tag rewrite is allowed.

- [x] **Step 6: Write candidate identity into final output and validate it before lock generation**

Rename the manifest step to `Create and verify candidate multi-architecture manifests` and add to `publish_summary`:

```python
"candidate_id": plan["candidate_id"],
```

The summary validator command must append:

```python
"--candidate-id",
plan["candidate_id"],
```

The lock generator command must append the same pair. Keep manifest creation targeted at `image["deploy_ref"]`, which Task 1 made run-unique, and retain raw digest/size/media-type/child-digest validation.

- [x] **Step 7: Upload the attempt-qualified complete candidate artifact before updating stable aliases**

Keep the pinned `Upload publish artifacts` action immediately after summary/lock validation and set its artifact name to:

```yaml
name: publish-${{ inputs.stream }}-${{ github.run_id }}-${{ github.run_attempt }}
```

Do not set `overwrite: true`. Add this separate step afterward:

```yaml
- name: Update convenience stream aliases
  run: |
    python3 - <<'PY'
    import json
    import pathlib
    import re
    import subprocess

    DIGEST_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
    plan = json.loads(
        pathlib.Path("artifacts/plan/publish-plan.json").read_text(encoding="utf-8")
    )
    summary = json.loads(
        pathlib.Path(plan["publish_summary_file"]).read_text(encoding="utf-8")
    )
    if summary.get("candidate_id") != plan.get("candidate_id"):
        raise SystemExit("publish summary candidate ID does not match frozen plan")
    planned_images = plan.get("images")
    summary_images = summary.get("images")
    if not isinstance(planned_images, list) or not isinstance(summary_images, list):
        raise SystemExit("candidate plan and summary images must be lists")
    summary_by_name = {entry.get("image"): entry for entry in summary_images}
    if len(summary_by_name) != len(summary_images):
        raise SystemExit("publish summary contains duplicate image names")
    if set(summary_by_name) != {entry.get("image") for entry in planned_images}:
        raise SystemExit("publish summary images do not match frozen plan")

    for planned_image in planned_images:
        image_name = planned_image["image"]
        summary_image = summary_by_name[image_name]
        stream_ref = planned_image["stream_ref"]
        candidate_ref = summary_image["deploy_ref"]
        if candidate_ref != planned_image["deploy_ref"]:
            raise SystemExit(f"candidate ref mismatch: {image_name}")
        repository, separator, candidate_tag = candidate_ref.rpartition(":")
        stream_repository, stream_separator, stream_tag = stream_ref.rpartition(":")
        if (
            not separator
            or not stream_separator
            or not candidate_tag
            or not stream_tag
            or repository != stream_repository
        ):
            raise SystemExit(f"candidate/stream ref mismatch: {image_name}")
        manifest_digest = summary_image["manifest_digest"]
        if not isinstance(manifest_digest, str) or not DIGEST_RE.fullmatch(manifest_digest):
            raise SystemExit(f"candidate digest is invalid: {image_name}")
        immutable_ref = f"{repository}@{manifest_digest}"
        immutable_raw = subprocess.run(
            ["docker", "buildx", "imagetools", "inspect", "--raw", immutable_ref],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "docker", "buildx", "imagetools", "create",
                "--tag", stream_ref, immutable_ref,
            ],
            check=True,
        )
        stream_raw = subprocess.run(
            ["docker", "buildx", "imagetools", "inspect", "--raw", stream_ref],
            check=True,
            capture_output=True,
        )
        if stream_raw.stdout != immutable_raw.stdout:
            raise SystemExit(f"stream alias bytes do not match candidate: {image_name}")
    PY
```

Do not add rollback logic. A failed alias write leaves the already uploaded candidate summary/manifest/lock artifact valid and makes the job fail visibly.

- [x] **Step 8: Run focused, full, and workflow syntax verification GREEN**

Run:

```bash
python3 -m unittest \
  tests.test_publish_summary_validation \
  tests.test_lock_generation \
  tests.test_publish_workflow -v
python3 -m unittest discover -s tests -v
actionlint .github/workflows/*.yml
```

Expected: unittest commands end with `OK` and `actionlint` emits no diagnostics.

- [x] **Step 9: Amend the single workspace commit**

```bash
git add scripts/validate-publish-summary.py scripts/generate-lock.py \
  .github/workflows/publish.yml tests/test_publish_summary_validation.py \
  tests/test_lock_generation.py tests/test_publish_workflow.py \
  docs/superpowers/plans/2026-07-14-publish-hardening.md
git commit --amend --no-edit
test "$(git rev-list --count origin/main..HEAD)" -eq 1
```

Expected: amend succeeds and exactly one local commit remains.

### Task 5: Operational Documentation, Boundary Tests, Complete Verification, and Final Review

**Files:**

- Modify: `README.md`
- Modify: `docs/publish.md`
- Modify: `docs/build-readiness.md`
- Modify: `docs/superpowers/specs/2026-07-14-publish-hardening-design.md`
- Modify: `docs/superpowers/specs/2026-07-13-kolla-multi-stream-ghcr-design.md`
- Modify: `docs/superpowers/plans/2026-07-13-kolla-multi-stream-ghcr.md`
- Modify: `docs/superpowers/specs/2026-07-14-kolla-compatible-candidate-lock-design.md`
- Modify: `docs/superpowers/plans/2026-07-14-kolla-compatible-candidate-lock.md`
- Modify: `tests/test_repository_boundary.py`
- Modify: `tests/test_publish_workflow.py`
- Modify: `docs/superpowers/plans/2026-07-14-publish-hardening.md`

**Interfaces:**

- Documents: real candidate refs such as `2025.1-rocky-9-candidate-123456789-1`, stable convenience aliases, downstream digest verification, current-run Kolla proof, local native Docker checks, and alias recovery.
- Documents: manual and CI publication both create a separate `workflow_dispatch` run; the CI dispatch credential has repository `Actions: write` only and does not supply candidate identity or inherit package-write authority.
- Documents: `publish-plan-<candidate-id>`, `native-<arch>-<candidate-id>`, and `publish-<stream>-<candidate-id>` artifact names, with **Re-run all jobs** as the only supported operator recovery procedure for a fresh attempt.
- Documents: each job-scoped Docker credential file is removed on normal completion without preempting Buildx post-job cleanup, while persistent runner cleanup/reimaging remains required for forced termination.
- Documents: a maximum of four online `kolla-build`-eligible native runners across both architectures; excess jobs queue.
- Preserves: the eight manual GitHub/GHCR prerequisites and the `openstack-infra-ops` ownership boundary.
- Produces: one clean, reviewed, locally committed branch with complete verification evidence and no external mutation.

- [x] **Step 1: Write RED documentation and responsibility-boundary tests**

Update `tests/test_repository_boundary.py` to require the candidate ref in operational examples:

```python
self.assertRegex(
    document,
    (
        r'(?m)^nova_compute_image_full:\s*"ghcr\.io/supergate-hub/'
        r'kolla-container-images/nova-compute:2025\.1-rocky-9-'
        r'candidate-123456789-1"\s*$'
    ),
)
```

Require these capacity/recovery tokens across `docs/publish.md` and `docs/build-readiness.md`:

```python
for document in (read_text(PUBLISH_DOC), read_text(READINESS_DOC)):
    self.assert_tokens(
        document,
        "candidate ID",
        "current Kolla summary",
        "local Unix socket",
        "candidate artifact",
        "stream alias",
        "openstack-infra-ops",
    )

readiness = read_text(READINESS_DOC)
self.assert_tokens(
    readiness,
    "maximum of four online",
    "kolla-build",
    "excess jobs queue",
    "max-parallel: 2",
)
```

Also require the exact attempt-qualified artifact patterns and rerun rule:

```python
for document in (read_text(PUBLISH_DOC), read_text(READINESS_DOC)):
    self.assert_tokens(
        document,
        "publish-plan-<candidate-id>",
        "native-<arch>-<candidate-id>",
        "publish-<stream>-<candidate-id>",
        "Re-run all jobs",
        "partial rerun",
        "fails closed",
    )
```

Replace the old reusable-trigger assertions in
`test_publish_doc_has_current_inputs_approval_and_job_contract` and the manual
prerequisite test with:

```python
self.assert_tokens(
    document,
    "workflow_dispatch",
    "separate workflow run",
    "Actions: write",
    "CI",
)
self.assertNotIn("workflow_call", document)
self.assertNotIn("packages: write ceiling", document)

self.assert_tokens(
    manual,
    "GITHUB_TOKEN",
    "packages: write",
    "Actions: write",
    "workflow_dispatch",
    "GitHub App",
    "no package-write permission",
    "--ref main",
    "protected ref",
)
```

Keep the exact five input names, exact eight numbered prerequisites, two
package-writing workflow jobs, and all approval phrase assertions unchanged.

Keep assertions rejecting digest-bearing or architecture-suffixed root `*_image_full` values and all environment lock/promotion/deployment patterns.

- [x] **Step 2: Run boundary/workflow tests and capture RED evidence**

Run:

```bash
python3 -m unittest \
  tests.test_repository_boundary \
  tests.test_publish_workflow -v
```

Expected: failures identify old stable-tag candidate examples and missing hard-cap/queue/current-summary/local-Docker/alias-recovery wording. Save the result in `.context/publish-hardening-red-task5.txt`.

- [x] **Step 3: Update operator-facing candidate, alias, and native-proof documentation**

Use the same representative references everywhere:

```text
AMD64 child:
ghcr.io/supergate-hub/kolla-container-images/nova-compute:2025.1-rocky-9-candidate-123456789-1-amd64

ARM64 child:
ghcr.io/supergate-hub/kolla-container-images/nova-compute:2025.1-rocky-9-candidate-123456789-1-arm64

Candidate multi-architecture ref used by the lock:
ghcr.io/supergate-hub/kolla-container-images/nova-compute:2025.1-rocky-9-candidate-123456789-1

Convenience stream alias, not used by the lock:
ghcr.io/supergate-hub/kolla-container-images/nova-compute:2025.1-rocky-9
```

Use this lock shape in current examples:

```yaml
_kolla_candidate_lock:
  images:
    "nova-compute":
      deploy_ref: "ghcr.io/supergate-hub/kolla-container-images/nova-compute:2025.1-rocky-9-candidate-123456789-1"
      manifest_digest: "sha256:<multi-arch-manifest-digest>"
      immutable_ref: "ghcr.io/supergate-hub/kolla-container-images/nova-compute@sha256:<multi-arch-manifest-digest>"
nova_compute_image_full: "ghcr.io/supergate-hub/kolla-container-images/nova-compute:2025.1-rocky-9-candidate-123456789-1"
```

Every manual lock-generation example must pass the same expected identity:

```bash
python3 scripts/generate-lock.py \
  --publish-summary artifacts/publish-summary-2025.1-rocky-9.json \
  --stream 2025.1-rocky-9 \
  --profile deployment \
  --candidate-id 123456789-1 \
  --output artifacts/kolla-ansible-image-lock-2025.1-rocky-9.yml
```

Replace the obsolete reusable-caller permission sentence in manual prerequisite
4 with this operational split: the dispatched publish workflow's own two jobs
retain `packages: write`; CI dispatch uses a repository-scoped GitHub App
installation token (or equivalent short-lived credential) with `Actions:
write` and no package-write permission. Show the safe dry-run CLI shape
`gh workflow run publish.yml --ref main --field stream=2025.1-rocky-9 --field profile=core --field image=keystone --field dry_run=true`, and require
`ghcr-publish` branch rules to admit only that reviewed protected ref. Keep
this as one numbered prerequisite so the list remains exactly eight items.

State explicitly that:

- a workflow run derives candidate ID from `github.run_id` and `github.run_attempt`, while local read-only planning uses `local-dry-run`;
- manual operators and CI automation both start `workflow_dispatch` as a separate workflow run; CI uses a repository-scoped GitHub App installation token or equivalent credential with `Actions: write`, and never passes candidate ID;
- CI dispatches `publish.yml` with `--ref main` (or the corresponding Actions API `ref: main`), and the protected environment restricts publication to that reviewed protected ref rather than arbitrary branches;
- the current Kolla summary is deleted before the invocation and validated before any remote tag is accepted;
- the native Docker endpoint is normalized to a verified local Unix socket with matching Linux server architecture;
- each package-writing job uses a fresh `DOCKER_CONFIG` below `RUNNER_TEMP`, removes `config.json` through an `always()` cleanup without preempting Buildx post-job cleanup, and still relies on runner cleanup/reimaging if the process is forcibly terminated;
- `publish-<stream>-<candidate-id>` is uploaded before stream aliases change;
- all upload/download names contain candidate ID; a full rerun creates a new coherent attempt, while a partial rerun without its upstream producer fails closed and operators use **Re-run all jobs** as the supported recovery procedure;
- a partial stream-alias failure fails the workflow but cannot invalidate the candidate lock, and retry creates a new candidate ID;
- `openstack-infra-ops` still verifies tag bytes/digest before consumption and owns environment selection/promotion/deployment/rollback.

- [x] **Step 4: Clarify the physical runner ceiling without inventing a workflow semaphore**

Replace the ambiguous capacity language with this policy in both operational docs:

```text
The total pool eligible for the `kolla-build` label is hard-capped at a
maximum of four online native runners across AMD64 and ARM64. One workflow
run still limits its two architecture legs with `max-parallel: 2`; when
cross-stream demand exceeds the physical pool, excess jobs queue. GitHub
Actions does not provide a repository counting semaphore in this workflow,
so the physical eligible-pool cap is the concurrency ceiling.
```

Keep the `150 GiB` minimum, `300 GB` operational target, Actions Runner `2.327.1` minimum, and native ARM64/QEMU policy.

- [x] **Step 5: Record the post-review addendum and update superseded examples without changing scope ownership**

Append a clearly dated **Post-review execution addendum** to
`docs/superpowers/specs/2026-07-14-publish-hardening-design.md`. State that the
approved candidate ID and publication sequence remain unchanged, while final
review tightened four execution details:

1. `workflow_dispatch` is the sole trigger and CI creates a separate dispatch
   run; `workflow_call` is removed to avoid shared caller run identity.
2. Every artifact name contains candidate ID; only **Re-run all jobs** creates
   a coherent new attempt and partial reruns fail closed.
3. Both package-writing jobs use fresh `DOCKER_CONFIG` directories and remove
   only `config.json` in an `always()` step so Buildx post-job cleanup still
   runs.
4. The exact extracted Kolla summary source and its per-version provenance are
   committed as test fixtures, including native ARM64 success and malformed
   plan coverage.

In the four older spec/plan files listed for this task, add a short supersession note pointing to `docs/superpowers/specs/2026-07-14-publish-hardening-design.md`, replace current stable-tag lock examples with `candidate-123456789-1`, and state that the stable stream tag is only a post-artifact convenience alias. Do not rewrite unrelated historical implementation steps.

Where those files describe `workflow_call` as a current requirement, mark that
trigger contract superseded too: current CI automation starts a separate
`workflow_dispatch` run to preserve the exact run-derived candidate identity
and run-scoped artifact namespace. Keep historical prose visibly identified as
superseded rather than silently changing the record.

- [x] **Step 6: Run documentation and full unit verification GREEN**

Run:

```bash
python3 -m unittest \
  tests.test_repository_boundary \
  tests.test_publish_workflow -v
python3 -m unittest discover -s tests -v
```

Expected: both commands exit `0` and end with `OK`.

- [x] **Step 7: Run every required repository verification**

Run exactly:

```bash
find . -type f -name '*.json' \
  -not -path './.git/*' \
  -not -path './.context/*' \
  -not -path './.superpowers/*' \
  -print0 | while IFS= read -r -d '' file; do
    python3 -m json.tool "$file" >/dev/null
  done

python3 scripts/validate-config.py

python3 scripts/plan-publish.py \
  --stream 2025.1-rocky-9 --profile core \
  --candidate-id local-dry-run --dry-run > .context/core-plan.json
python3 scripts/plan-publish.py \
  --stream 2025.1-rocky-9 --profile deployment \
  --candidate-id local-dry-run --dry-run > .context/deployment-plan.json
python3 scripts/plan-publish.py \
  --stream 2025.1-rocky-9 --profile core --image keystone \
  --candidate-id local-dry-run --dry-run > .context/keystone-plan.json
python3 scripts/plan-publish.py \
  --stream 2025.1-ubuntu-noble --profile deployment \
  --candidate-id local-dry-run --dry-run > .context/ubuntu-deployment-plan.json

python3 -m unittest discover -s tests -v
actionlint .github/workflows/*.yml

legacy_owner='supergate-''jhbyun'
if rg -n --hidden --glob '!.git/**' --glob '!.context/**' \
  --glob '!.superpowers/**' "$legacy_owner" .; then
  echo "legacy personal namespace remains" >&2
  exit 1
fi

if rg -n --glob '*.md' \
  '^[a-z0-9_]+_image_full:.*@sha256:' README.md docs; then
  echo "unsafe digest-bearing Kolla-Ansible root reference remains" >&2
  exit 1
fi

git diff --check origin/main...
git status --short
git rev-list --left-right --count origin/main...HEAD
```

Expected: every command exits `0`; JSON parsing and `actionlint` are silent; configuration validation passes; all four plan files parse as JSON; unittest ends with `OK`; both searches return no match; `git diff --check` is silent; the branch remains unchanged and one commit ahead of `origin/main`.

- [x] **Step 8: Inspect representative outputs and exact responsibility boundary**

Run:

```bash
python3 - <<'PY'
import json
from pathlib import Path

plan = json.loads(Path('.context/deployment-plan.json').read_text())
image = next(entry for entry in plan['images'] if entry['image'] == 'nova-compute')
print(plan['candidate_id'])
print(image['architectures'][0]['arch_ref'])
print(image['architectures'][1]['arch_ref'])
print(image['deploy_ref'])
print(image['stream_ref'])
print(plan['kolla_ansible_lock_file'])
PY
```

Expected exact output:

```text
local-dry-run
ghcr.io/supergate-hub/kolla-container-images/nova-compute:2025.1-rocky-9-candidate-local-dry-run-amd64
ghcr.io/supergate-hub/kolla-container-images/nova-compute:2025.1-rocky-9-candidate-local-dry-run-arm64
ghcr.io/supergate-hub/kolla-container-images/nova-compute:2025.1-rocky-9-candidate-local-dry-run
ghcr.io/supergate-hub/kolla-container-images/nova-compute:2025.1-rocky-9
artifacts/kolla-ansible-image-lock-2025.1-rocky-9.yml
```

Confirm no source/config/workflow file contains an environment lock path, promotion action, deployment command, or rollback logic.

- [x] **Step 9: Request two-stage final review and resolve only in-scope findings**

Run the `superpowers:requesting-code-review` workflow against `origin/main...HEAD`, asking reviewers to check:

```text
1. Current Kolla summary must gate all remote-tag evidence.
2. Candidate identity must be run-derived and frozen through plan/approval/summary/lock.
3. Dispatch-only isolation and attempt-qualified artifact names must prevent caller/rerun collisions.
4. Candidate artifacts must precede stable alias writes.
5. All Action SHAs, checkout settings, and Docker credential cleanup must match the allowlist and lifecycle contract without breaking Buildx post cleanup.
6. Native Docker must fail closed on remote/non-Linux/wrong-architecture endpoints.
7. No environment promotion/deployment responsibility may enter this repository.
```

Expected: specification and code-quality reviews return no unresolved Critical or Important findings. Address in-scope findings with focused tests and rerun Step 7.

- [x] **Step 10: Amend once more and record final local state**

```bash
git add README.md docs/publish.md docs/build-readiness.md \
  docs/superpowers/specs/2026-07-14-publish-hardening-design.md \
  docs/superpowers/specs/2026-07-13-kolla-multi-stream-ghcr-design.md \
  docs/superpowers/plans/2026-07-13-kolla-multi-stream-ghcr.md \
  docs/superpowers/specs/2026-07-14-kolla-compatible-candidate-lock-design.md \
  docs/superpowers/plans/2026-07-14-kolla-compatible-candidate-lock.md \
  docs/superpowers/plans/2026-07-14-publish-hardening.md \
  tests/test_repository_boundary.py tests/test_publish_workflow.py
git commit --amend --no-edit
test "$(git rev-list --count origin/main..HEAD)" -eq 1
git status --short
git rev-parse HEAD
```

Expected: amend succeeds, the commit-count assertion exits `0`, `git status --short` is empty, and the final local commit SHA is printed for the completion report.

## Completion Report Contract

Report all of the following in Korean after Task 5 completes:

- changed files grouped by candidate planning, Kolla proof, workflow hardening, summary/lock semantics, tests, and documentation;
- RED evidence for all five tasks and GREEN evidence with the final exact unittest count;
- representative real-run candidate child refs, candidate multi-architecture ref, stable convenience alias, and candidate-lock root ref;
- required manual GitHub/GHCR settings: maximum four online native runners, protected environment/reviewers/branch restrictions, three scope variables, `GITHUB_TOKEN` package permission policy, repository-scoped CI dispatch credential with `Actions: write` and no package-write permission, package visibility/linkage, private-pull credentials, retention/scanning/cleanup, and exact approval phrase;
- confirmation that no real publish, dispatch, variable/environment change, push, or PR transition occurred;
- confirmation that generic candidate lock handoff remains terminal and `openstack-infra-ops` retains environment validation, promotion, deployment, and rollback ownership;
- remaining risks: first real native AMD64/ARM64 run, GHCR package policy/visibility, physical runner capacity, forced-termination runner cleanup, full-rerun-only recovery, matching-OS deployment smoke, downstream tag-to-digest verification, and non-transactional convenience aliases;
- final local commit SHA, unchanged branch name, clean status, and exactly one commit over `origin/main`.
