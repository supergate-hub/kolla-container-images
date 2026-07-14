# Kolla Publish Hardening Design

## Goal

Close the final publish-readiness findings without changing the repository's
terminal responsibility. A real run must prove that the current Kolla
invocation built every planned image, use a native local Docker daemon, avoid
persisting write credentials, pin third-party Actions immutably, and emit a
write-once generic candidate whose lock is not invalidated by partial updates
to convenience stream tags.

## Confirmed Problems

1. Pinned Kolla `20.4.0`, `21.1.0`, and `22.0.0` can exit zero with expected
   images unmatched, skipped, or unbuildable. The workflow currently requests
   a JSON build summary but does not validate it before inspecting remote
   architecture tags, so an older tag can be mistaken for current output.
2. Checkout credentials are persisted by default in jobs that receive
   `packages: write`.
3. Runner CPU architecture is checked, but the Docker daemon that performs
   build, pull, inspect, and smoke is not required to be the same native Linux
   architecture or a local endpoint.
4. Third-party Actions are selected through mutable major tags.
5. Architecture-neutral stream tags are updated one image at a time. A later
   failure can leave a mixed set and invalidate a lock that refers directly
   to those tags.

GitHub Actions has no counting semaphore for two to four jobs. Cross-stream
execution remains bounded by the physical self-hosted runner pool; the
readiness contract will state that the total `kolla-build`-eligible pool is
hard-capped at four online runners and excess jobs queue.

## Candidate Identity and References

The workflow derives one candidate ID from trusted GitHub run context:

```text
<github.run_id>-<github.run_attempt>
```

Only positive decimal components are accepted for a publish workflow. Local
read-only planning uses the reserved deterministic ID `local-dry-run` unless
an explicit candidate ID is supplied.

For stream `2025.1-rocky-9` and candidate ID `123456789-1`, one image has:

```text
AMD64 child:
ghcr.io/supergate-hub/kolla-container-images/nova-compute:2025.1-rocky-9-candidate-123456789-1-amd64

ARM64 child:
ghcr.io/supergate-hub/kolla-container-images/nova-compute:2025.1-rocky-9-candidate-123456789-1-arm64

Candidate multi-arch ref used by the lock:
ghcr.io/supergate-hub/kolla-container-images/nova-compute:2025.1-rocky-9-candidate-123456789-1

Convenience stream alias, never used by a candidate lock:
ghcr.io/supergate-hub/kolla-container-images/nova-compute:2025.1-rocky-9
```

The matrix tag policy gains explicit candidate and candidate-architecture
templates. The existing deploy template remains the convenience stream alias.
Every real workflow plan contains `candidate_id`, candidate architecture tags,
candidate `deploy_ref` values, and a separate `stream_ref` for the alias.

The plan job passes the trusted candidate ID explicitly. Authorization,
native build, and finalization require the frozen plan's ID to equal the same
run-derived value before any registry login. The count-bearing approval phrase
does not contain the run ID because it is entered before a dispatch receives
one; protected-environment review and runtime plan revalidation bind the
publication to the actual run.

Candidate IDs are never reused by this workflow. A GitHub rerun increments
`run_attempt`, so it receives new child and multi-architecture tags. This is
an application-level write-once contract; downstream digest verification
remains mandatory because GHCR tags are not intrinsically immutable.

## Publish Sequence

The native legs build and push only candidate architecture tags. Immediately
before invoking Kolla, the workflow removes the planned summary path so a
stale local file cannot satisfy validation. Immediately after Kolla exits, a
standard-library validator reads that exact file and fails unless:

- the document has exactly `built`, `failed`, `not_matched`, `skipped`, and
  `unbuildable` arrays;
- entries have the pinned schema, valid names, no duplicate or cross-bucket
  names, and no unexpected keys;
- `built` equals the exact union of planned parents and leaves for the current
  architecture;
- `failed`, `skipped`, and `unbuildable` are empty; and
- no planned name appears in `not_matched`.

Only then may the workflow inspect candidate architecture tags, pull their
immutable digests, run native smoke, and write evidence.

Finalization creates and fully validates every candidate multi-architecture
tag from recorded child digests. It writes and validates the complete publish
summary and, for exact `deployment/all`, the candidate lock. The lock uses the
candidate tag:

```yaml
_kolla_candidate_lock:
  images:
    "nova-compute":
      deploy_ref: "ghcr.io/supergate-hub/kolla-container-images/nova-compute:2025.1-rocky-9-candidate-123456789-1"
      manifest_digest: "sha256:<multi-arch-manifest-digest>"
      immutable_ref: "ghcr.io/supergate-hub/kolla-container-images/nova-compute@sha256:<multi-arch-manifest-digest>"
nova_compute_image_full: "ghcr.io/supergate-hub/kolla-container-images/nova-compute:2025.1-rocky-9-candidate-123456789-1"
```

The workflow uploads `publish-<stream>-<candidate-id>` with the validated
summary, raw candidate manifests, and eligible lock before a separate step updates
convenience stream aliases from the validated immutable candidate digests.
Alias writes remain sequential because GHCR cannot update repositories
transactionally. A partial alias failure makes the workflow fail and can be
retried, but the already uploaded candidate artifact remains internally
consistent: locks never point to convenience aliases. The repository adds no
alias rollback, environment pointer, or deployment logic.

## Native Docker Enforcement

After Buildx setup and before registry login, the native job checks the exact
Docker endpoint used by subsequent commands:

- Docker server `OSType` is `linux`;
- server `Architecture` equals the matrix's `x86_64` or `aarch64` value;
- the active Docker endpoint uses a local Unix socket, whether selected by the
  current context or `DOCKER_HOST`; and
- the check occurs after builder setup so later Kolla, pull, inspect, and smoke
  commands inherit the same environment.

No native-evidence schema field is added. The fail-closed pre-login check is
the evidence prerequisite, while existing evidence continues to record the
validated runner machine and platform.

## GitHub Actions Supply Chain and Credentials

Every `uses:` reference in publish and validation workflows is pinned to one
reviewed 40-character commit SHA. A trailing comment records its semantic
release, such as `# v7`, so automated review can identify intended upgrades.
Tests enforce an exact repository/SHA/release allowlist rather than accepting
arbitrary major tags.

Every checkout sets:

```yaml
with:
  persist-credentials: false
```

This applies to read-only jobs as one consistent policy and is mandatory in
both package-writing jobs.

## Failure and Recovery Semantics

- An incomplete or malformed Kolla summary fails before remote tag evidence
  can be accepted, even if older stream or candidate tags exist.
- A native or candidate-manifest failure can leave unreferenced, run-unique
  candidate tags. Retention policy may clean them later; no lock is emitted
  for an incomplete set.
- A convenience-alias failure can leave mixed aliases, but candidate locks and
  immutable identities remain valid. Rerunning creates a new candidate and
  retries aliases only after another complete validation.
- `openstack-infra-ops` verifies each candidate `deploy_ref` against the
  recorded digest and bytes immediately before Kolla-Ansible consumption.
  Environment selection, promotion, deployment, rollback, and site policy
  remain outside this repository.

## Testing and Verification

- Unit-test Kolla summary validation with the common schema proven by all
  three pinned releases: exact success, missing expected image, failed,
  skipped, unbuildable, planned name in `not_matched`, duplicate names,
  malformed entries, unexpected keys, and a stale remote-tag scenario whose
  current summary is incomplete.
- Test candidate-ID validation, all seven candidate tag shapes, stable
  `stream_ref` separation, and frozen-plan approval revalidation.
- Test workflow ordering: summary deletion and validation precede any remote
  architecture inspection; complete candidate summary/lock validation
  precedes stream-alias updates.
- Test all checkouts for disabled credential persistence, all Actions against
  the exact SHA allowlist, and native Docker OS/architecture/local-endpoint
  checks before login.
- Clarify and test the hard four-runner capacity ceiling.
- Re-run every JSON check, configuration validator, required Rocky and Ubuntu
  dry-run plans, full unit suite, `actionlint`, namespace and unsafe-reference
  searches, and Git diff checks.

## Scope Boundary

This change performs no registry publication, workflow dispatch, runner or
environment mutation, repository-variable change, push, or PR transition. It
adds no external dependency and no Dev/Stg/Prod lock, promotion, environment
pointer, deployment action, rollback, or site secret. The terminal flow stays:

```text
build candidate children -> validate current Kolla summary
  -> create and validate candidate manifests -> publish summary
  -> generic candidate lock -> optional convenience stream aliases
  -> hand off to openstack-infra-ops
```

## Post-review execution addendum (2026-07-14)

The approved candidate ID and publication sequence above remain unchanged.
Final review tightened four execution details:

1. `workflow_dispatch` is the sole trigger. Manual operators and CI each
   create a separate dispatch run; `workflow_call` is removed so caller and
   publisher cannot share run identity.
2. Every artifact name contains candidate ID. Only **Re-run all jobs** creates
   a coherent new attempt; a partial rerun without its producer fails closed.
3. Both package-writing jobs create fresh `DOCKER_CONFIG` directories below
   `RUNNER_TEMP` and remove only `config.json` in an `always()` step after
   ordinary steps, preserving Buildx post-job cleanup. Forced termination
   still requires persistent-runner cleanup or reimaging.
4. The exact extracted Kolla summary source and per-version provenance are
   committed as test fixtures. Coverage includes native ARM64 success and
   malformed publish-plan cases as well as the shared summary schema.
