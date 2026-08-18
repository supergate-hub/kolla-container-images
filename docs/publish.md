# Publish workflow

`.github/workflows/publish.yml` is a manual/CI `workflow_dispatch` workflow for
planning or publishing exact-version Kolla image streams. CI starts a separate
workflow run with a repository-scoped GitHub App token (or equivalent
short-lived credential) that has `Actions: write` and no package-write
permission. Candidate identity is always derived from
`github.run_id`-`github.run_attempt`; callers cannot provide it.

```text
freeze plan -> environment authorization -> native revision children
            -> revision multi-architecture manifests
            -> validated summary + eligible generic candidate lock upload
            -> semantic aliases -> hand off to openstack-infra-ops
```

## Inputs

The form contains exactly three inputs:

| Input | Contract |
| --- | --- |
| `operation` | Choice `plan` or `publish`; default `plan` |
| `stream` | Exact ID from the branch-local schema-v4 matrix |
| `scope` | Choice `keystone`, `core`, or `deployment`; default `keystone` |

The scope mapping is fixed:

```text
keystone   -> core / keystone
core       -> core / all
deployment -> deployment / all
```

The previous `profile`, `image`, `dry_run`, and `approval` fields are removed.
There is no typed approval phrase. Arbitrary profiles and individual images
remain a local planner feature and are not a publication surface.

Render a workflow plan from any reviewed ref:

```bash
gh workflow run publish.yml \
  --ref 2025-1 \
  --field operation=plan \
  --field stream=2025.1-rocky-10.2-20.5.0 \
  --field scope=keystone
```

`operation=plan` validates configuration, resolves the OS tag once, creates
`publish-plan-<candidate-id>`, and writes a summary containing branch/SHA,
stream, scope/count, Kolla and Kolla-Ansible commits, source-set identity and
digest, resolved base index/child digests, and semantic/revision refs. It does
not request the publish environment, log in to GHCR, build, push, create a
publish summary, generate a lock, or mutate a registry.

Default OS aliases are configured once in the matrix `tag_aliases` map. The
planner carries the matching alias refs into every plan, and the finalizer
updates and verifies them from the immutable revision digest automatically;
they are not workflow-dispatch inputs.

## Publish authorization

Only a protected release branch may publish:

```text
2025.1 streams -> refs/heads/2025-1
2025.2 streams -> refs/heads/2025-2
2026.1 streams -> refs/heads/2026-1
```

`main`, tags, feature branches, the wrong release branch, mixed-release local
configuration, and `publish_enabled: false` all fail closed. Every writer
revalidates the frozen plan, candidate ID, protected ref, and kill switch after
the `ghcr-publish` environment gate.

Publication is selected explicitly with `operation=publish`; changing `scope`
alone never turns a plan into a writer.

| Scope input | Frozen scope | Required repository variable |
| --- | --- | --- |
| `keystone` | `core/keystone` | `ALLOW_GHCR_PUBLISH=true` |
| `core` | `core/all` | `ALLOW_GHCR_FULL_CORE_PUBLISH=true` |
| `deployment` | `deployment/all` | `ALLOW_GHCR_DEPLOYMENT_PUBLISH=true` |

Plan and publish concurrency are separate. Pending plan runs for the same
ref/stream may cancel older plans. Publish scopes for one release
branch/stream are serialized and an in-progress publish is never cancelled by
a plan or another publish. GitHub's `queue: max` retains up to 100 pending
publishes for that writer group instead of replacing the older pending run.

## Frozen inputs

The plan joins a release, version-keyed toolchain, configured base, and
OpenStack source-set. It contains:

- exact OpenStack Releases, Kolla, and Kolla-Ansible commits;
- the complete OpenStack source-set document and its canonical digest;
- deterministic `kolla-build.conf` and template-override content/digests;
- commit-addressed and checksum-verified direct Dockerfile artifacts, plus
  checksum-verified Kolla Toolbox constraints;
- the requested OS image tag, resolved OCI index digest, and exact
  index manifest proof bytes and `linux/amd64`/`linux/arm64` child digests;
- exact build DAG, scope, image count, semantic refs, revision refs, native
  revision refs, and evidence paths.

Active source-set schema v3 binds every compatible toolchain version to its
exact Kolla and Kolla-Ansible commits, pinned `sources.py` digest, and normalized
source closure digest. A missing or changed toolchain entry requires a new
append-only source-set revision. Before rendering a plan, the workflow checks
out the exact matrix-pinned `openstack/releases` commit and proves both
toolchain commits against its deliverables.

`stable/YYYY.N` source refs are tracking metadata only. Kolla receives exact
project `build_commit` SHAs. Before registry login, each unit fetches only its
required project commits without remote tags, derives PBR versions from the
frozen release tag and ancestry with the hash-locked build engine, and creates
sorted, normalized local source archives without `.git` metadata. Kolla never
clones a moving upstream source. The unit validates the pinned Kolla source
closure before writing the frozen configuration. A missing
service/addition/plugin or moving source reference fails before registry
mutation. Reviewed inline
downloads such as `ovn-ctl` and MariaDB `clustercheck` are replaced by
commit-addressed URLs with in-build SHA-256 verification.

The base tag is resolved only by the plan job. A native unit pulls
`requested-repository@child-digest`, verifies its platform and digest, retags
it to the configured base tag, and runs Kolla with Kolla's upstream `--nopull`
option. Thus an
upstream tag move during the workflow cannot change that run's base.
DNF/APT repository snapshots are not implemented, so package-level complete
rebuild reproducibility remains future work.

## Publication sequence

An approved publish uses native standard hosted runners:

```text
AMD64: ubuntu-24.04     (x86_64, linux/amd64)
ARM64: ubuntu-24.04-arm (aarch64, linux/arm64)
```

The staged jobs are:

1. `publish-plan` renders and uploads the frozen plan.
2. `authorize-publish` crosses the protected `ghcr-publish` environment and
   validates the scope kill switch.
3. `build-parent-tier-0`, `build-parent-tier-1`, and
   `build-parent-tier-2` build one frozen target per native job.
4. `build-leaf-stage-0` and the optional `build-leaf-stage-1` build selected
   leaves. Stage 1 represents selected-leaf dependencies such as
   `ovn-sb-db-server -> ovn-sb-db-relay`.
5. `collect-native-evidence` validates the complete per-architecture closure.
6. `finalize-publish` creates and verifies revision manifests, validates a
   schema-v3 summary, and generates a schema-v3 lock only for
   `deployment/all`.
7. The terminal candidate artifact is uploaded before semantic aliases move.
8. Semantic aliases are created from immutable revision digests and their raw
   manifest bytes are reverified.

All matrices use `max-parallel: 4`. Each build command is anchored to one
target with `--threads 1`, `--push-threads 1`, and `--nopull`. Before registry
login, the installed exact Kolla parser validates the complete frozen argv and
proves that `pull=False`.
Each dependent unit pulls ancestors through immutable digests recorded in raw
unit evidence, verifies and retags them locally, proves that its own revision
tag is absent, and then uses `--skip-existing` to skip only those proven
ancestors. `--skip-parents` is forbidden because it can also skip the selected
target. There is no parent-index artifact.

Do not use **Re-run failed jobs**. The run attempt participates in candidate
and revision identity, so partial reruns fail closed against a mixed evidence
set. Use **Re-run all jobs**, which creates a new candidate ID and a coherent
revision.

## Tags, summary, and lock

For candidate `123456789-1`, the Nova Compute refs of
`2025.1-rocky-10.2-20.5.0` are:

```text
semantic_ref:
ghcr.io/supergate-hub/kolla-container-images/nova-compute:2025.1-rocky-10.2-20.5.0

revision_ref (used by the lock):
ghcr.io/supergate-hub/kolla-container-images/nova-compute:2025.1-rocky-10.2-20.5.0-rev-123456789-1

revision_arch_ref:
ghcr.io/supergate-hub/kolla-container-images/nova-compute:2025.1-rocky-10.2-20.5.0-rev-123456789-1-amd64
ghcr.io/supergate-hub/kolla-container-images/nova-compute:2025.1-rocky-10.2-20.5.0-rev-123456789-1-arm64
```

The semantic contract is
`{release}-{distro}-{os_version}-{kolla_ansible_version}`. Candidate-qualified
image tags do not exist; candidate ID still binds Actions artifacts and
evidence. Existing major/codename tags remain in GHCR but are neither updated
nor used as aliases for new exact-version streams.

The finalizer accepts only OCI image-index or Docker manifest-list media types
with exactly `linux/amd64` and `linux/arm64` children matching native evidence.
It hashes raw `repository@digest` bytes and validates descriptor digest,
media type, and size.

The accepted media types are exactly
`application/vnd.oci.image.index.v1+json` and
`application/vnd.docker.distribution.manifest.list.v2+json`.

Artifact names and terminal paths are deterministic:

| Artifact | Contents |
| --- | --- |
| `publish-plan-<candidate-id>` | `artifacts/plan/publish-plan.json` |
| `unit-evidence-<arch>-<kind>-<target>-<candidate-id>` | One unit's schema-v3 evidence |
| `unit-diagnostics-<unit-id>-<candidate-id>` | One-day failure-only logs and local build diagnostics for a failed unit |
| `native-amd64-<candidate-id>` | `artifacts/arch/native-amd64.json` |
| `native-arm64-<candidate-id>` | `artifacts/arch/native-arm64.json` |
| `publish-<stream>-<candidate-id>` | `artifacts/publish-summary-<stream>.json`, `artifacts/manifests/`, and an eligible `artifacts/kolla-ansible-image-lock-<stream>.yml` |

Only `deployment/all` may produce the generic candidate lock. Keystone, core,
partial deployment, incomplete evidence, or invalid provenance cannot produce
one. The terminal upload precedes semantic alias updates; therefore an alias
write failure leaves the revision manifest and lock valid.

Lock schema v3 stores semantic and revision refs, manifest and child digests,
immutable refs, full OpenStack source-set provenance, deterministic config
digests, Kolla/Kolla-Ansible commits, and frozen base digests. Root-level
Kolla-Ansible variables intentionally use `revision_ref`:

```yaml
_kolla_candidate_lock:
  schema_version: 3
  candidate_id: "123456789-1"
  stream: "2025.1-rocky-10.2-20.5.0"
  base:
    id: "rocky-10.2"
    requested_ref: "quay.io/rockylinux/rockylinux:10.2"
    index_digest: "sha256:<base-index-digest>"
    platforms: {amd64: {digest: "sha256:<base-amd64>"}, arm64: {digest: "sha256:<base-arm64>"}}
  openstack_sources:
    source_set: {id: "epoxy-20260813-r1", projects: "<full pinned mapping>"}
    canonical_digest: "sha256:<source-set-digest>"
    kolla_build_config_sha256: "sha256:<config-digest>"
    template_override_sha256: "sha256:<override-digest>"
  images:
    nova-compute:
      semantic_ref: "ghcr.io/supergate-hub/kolla-container-images/nova-compute:2025.1-rocky-10.2-20.5.0"
      revision_ref: "ghcr.io/supergate-hub/kolla-container-images/nova-compute:2025.1-rocky-10.2-20.5.0-rev-123456789-1"
      manifest_digest: "sha256:<manifest-digest>"
      immutable_ref: "ghcr.io/supergate-hub/kolla-container-images/nova-compute@sha256:<manifest-digest>"
nova_compute_image_full: "ghcr.io/supergate-hub/kolla-container-images/nova-compute:2025.1-rocky-10.2-20.5.0-rev-123456789-1"
```

`openstack-infra-ops` verifies `revision_ref` and `immutable_ref` against the
recorded bytes and digest before deployment. The root variables are
architecture-neutral revision tags; Docker or Podman selects the correct child
on homogeneous and mixed-architecture clusters. They are never architecture
tags or digest-bearing values.

## Manual GitHub and GHCR prerequisites

1. Keep the repository **Public** and allow standard `ubuntu-24.04` and
   `ubuntu-24.04-arm` runners. Do not substitute billed larger runners. Verify
   Organization package creation allows Public GHCR packages.
2. Create protected `2025-1`, `2025-2`, and `2026-1` branches with PR review,
   required validation, conversation resolution, no bypass, and force-push /
   delete disabled. `main`은 publish ref로 사용할 수 없다. The workflow also
   requires `github.ref_protected == true`.
3. Configure the `ghcr-publish` environment with required reviewers and allow
   only those three branches; exclude `main` and tags.
4. Create `ALLOW_GHCR_PUBLISH`, `ALLOW_GHCR_FULL_CORE_PUBLISH`, and
   `ALLOW_GHCR_DEPLOYMENT_PUBLISH`. Keep all false outside an approved window
   and enable only the required scope.
5. Keep repository-wide Actions permissions read-only. Grant job-scoped
   `packages: write` only to native build writers and finalization. External CI
   dispatch uses `Actions: write` with no package-write permission.
6. Validate aggregate changes on `main`, create release-local matrix/source-set
   commits from the reviewed merge, and run `operation=plan` for every
   branch-local stream and scope. Inspect source/base digests and the exact
   eight-unit Keystone closure before publishing.
7. First publish only
   `2025-1 / 2025.1-rocky-10.2-20.5.0 / keystone` with
   `ALLOW_GHCR_PUBLISH=true`. Approve it through `ghcr-publish`, then require
   the 8 GiB preflight, 2 GiB observed minimum, native evidence, exact
   two-platform revision manifest, summary, and semantic-alias digest check.

   ```bash
   gh workflow run publish.yml \
     --ref 2025-1 \
     --field operation=publish \
     --field stream=2025.1-rocky-10.2-20.5.0 \
     --field scope=keystone
   ```

8. After the first push, verify every GHCR package is linked to this repository,
   explicitly Public, and anonymously inspectable/pullable with an empty
   Docker config. Apply retention, vulnerability scanning, and cleanup policy,
   restore the kill switch to false, then expand separately to core and
   deployment.

The checklist does not prove that current GitHub settings exist. Re-query
branch protection, environment reviewers, kill switches, package visibility
and linkage immediately before each publish, and preserve the observed result
with the run.

## Handoff and secret boundary

The validated publish summary and generic candidate lock are the terminal
outputs. `openstack-infra-ops` copies and validates the lock, chooses an
environment, creates environment-specific locks or pointers, runs matching-OS
deployment smoke, and owns promotion, deployment, and rollback.

This repository does not create Dev/Stg/Prod state or run Kolla-Ansible
deployment actions. Registry credentials, OpenStack credentials, Ceph keys,
private CAs, kubeconfigs, and site-specific configuration are never embedded
in images or generated candidate locks.
