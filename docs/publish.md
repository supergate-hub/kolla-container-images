# Publish workflow

`.github/workflows/publish.yml` has `workflow_dispatch` as its sole trigger.
Manual operators and CI automation each create a separate workflow run:

```text
freeze plan -> authorize -> publish native children -> create multi-architecture manifests
            -> publish summary -> generic candidate lock when eligible
            -> hand off to openstack-infra-ops
```

The plan job rejects a repository identity other than
`supergate-hub/kolla-container-images` before checkout. Every workflow run
derives its candidate ID from `github.run_id` and `github.run_attempt`; neither
manual operators nor CI pass candidate identity. Local read-only planning uses
the reserved `local-dry-run` candidate ID.

CI dispatches `publish.yml` on the reviewed protected ref with `--ref main`
(or Actions API `ref: main`). Its repository-scoped GitHub App installation
token, or equivalent short-lived credential, has `Actions: write` and no
package-write permission. The dispatched workflow's own `build-native` and
`finalize-publish` jobs separately receive `packages: write` from their
`GITHUB_TOKEN` policy.

## Inputs and dry-run default

`workflow_dispatch` exposes exactly these five frozen-scope inputs:

| Input | Contract |
| --- | --- |
| `stream` | Exact ID from `config/build-matrix.json`; free-form release/base combinations are invalid |
| `profile` | `core` or `deployment` |
| `image` | One resolved leaf, or `all` |
| `dry_run` | Boolean; defaults to `true` |
| `approval` | Ignored for a dry run; must exactly match the frozen plan for publication |

Keep `dry_run: true` for routine planning:

```bash
gh workflow run publish.yml \
  --ref main \
  --field stream=2025.1-rocky-9 \
  --field profile=core \
  --field image=keystone \
  --field dry_run=true
```

The `publish-plan` job validates the repository configuration, renders one
frozen publish plan, and uploads `artifacts/plan/publish-plan.json` as
`publish-plan-<candidate-id>`. With `dry_run: true`, no authorization, registry
login, build, push, manifest, or lock-generation job runs.

## Publication approval

Only three non-dry-run scopes exist. The frozen publish plan supplies their
required repository variable, resolved count, and exact approval phrase:

| Scope | Required repository variable |
| --- | --- |
| `core/keystone` | `ALLOW_GHCR_PUBLISH=true` |
| `core/all` | `ALLOW_GHCR_FULL_CORE_PUBLISH=true` |
| `deployment/all` | `ALLOW_GHCR_DEPLOYMENT_PUBLISH=true` |

For the standing stream, the three exact phrases are:

```text
PUBLISH ghcr.io/supergate-hub/kolla-container-images 2025.1-rocky-9 core/keystone (1 image, amd64/arm64)
PUBLISH ghcr.io/supergate-hub/kolla-container-images 2025.1-rocky-9 core/all (21 images, amd64/arm64)
PUBLISH ghcr.io/supergate-hub/kolla-container-images 2025.1-rocky-9 deployment/all (63 images, amd64/arm64)
```

Counts vary by resolved stream, so operators must copy the count-bearing
phrase from that run's frozen publish plan instead of adapting an example. All
other partial-image scopes remain dry-run only. An approval for one stream or
scope cannot authorize another.

Every `dry_run: false` run also crosses the protected GitHub environment
`ghcr-publish`, where required reviewers and branch/tag restrictions are
configured manually. `authorize-publish` validates the three repository
variables and exact phrase against the frozen plan. `build-native` and
`finalize-publish` revalidate that approval before their registry login. Only
those two jobs receive `packages: write`; planning and
authorization remain read-only.

## Publication sequence

Writers for the same stream use `kolla-publish-<stream>` concurrency and do not
cancel an in-progress run. An approved publication follows four jobs:

1. `publish-plan` freezes and uploads the validated plan.
2. `authorize-publish` crosses `ghcr-publish` and binds approval to that plan.
3. `build-native` runs one static AMD64/ARM64 matrix with `max-parallel: 2`,
   installs the matrix-pinned Kolla package with `docker==7.1.0`, verifies the
   SDK import/version and Kolla CLI before login, publishes the selected
   parents and leaves, inspects each remote descriptor, pulls it by digest,
   and records native evidence.
4. `finalize-publish` downloads both exact evidence sets, creates each
   multi-architecture manifest from recorded child digests, validates the
   manifest and summary, and generates a lock only for `deployment/all`.

Before Kolla runs, each native leg deletes the planned summary file. It accepts
no remote tag evidence until the current Kolla summary validates against the
frozen plan. It also normalizes Docker to a verified local Unix socket and
requires a Linux server whose architecture matches the native runner.

Each package-writing job creates a fresh `DOCKER_CONFIG` below `RUNNER_TEMP`.
Its final `always()` step removes only `config.json`, after all ordinary steps,
so Buildx post-job cleanup is not preempted. Forced termination can bypass that
step; persistent runners therefore still require cleanup or reimaging before
reuse.

The native matrix uses these self-hosted runner labels:

```text
self-hosted, linux, x64, kolla-build
self-hosted, linux, ARM64, kolla-build
```

Both self-hosted pools require Actions Runner 2.327.1 or newer for the selected
Node 24 action releases.

The total pool eligible for the `kolla-build` label is hard-capped at a
maximum of four online native runners across AMD64 and ARM64. One workflow run
still limits its two architecture legs with `max-parallel: 2`; when
cross-stream demand exceeds the physical pool, excess jobs queue. GitHub
Actions does not provide a repository counting semaphore in this workflow, so
the physical eligible-pool cap is the concurrency ceiling.

Runner capacity and evidence requirements are detailed in
[build-readiness.md](build-readiness.md).

## Artifacts and validation

Artifact names and terminal paths are deterministic:

| Artifact | Contents |
| --- | --- |
| `publish-plan-<candidate-id>` | `artifacts/plan/publish-plan.json` |
| `native-amd64-<candidate-id>` | `artifacts/arch/native-amd64.json` |
| `native-arm64-<candidate-id>` | `artifacts/arch/native-arm64.json` |
| `native-diagnostics-amd64-<candidate-id>` | AMD64 Kolla summaries and logs |
| `native-diagnostics-arm64-<candidate-id>` | ARM64 Kolla summaries and logs |
| `publish-<stream>-<candidate-id>` | `artifacts/publish-summary-<stream>.json`, `artifacts/manifests/`, and the candidate lock when eligible |

The diagnostics artifacts are separate so the download layout of each exact
`native-<arch>-<candidate-id>` evidence artifact remains unchanged. All upload
and download names include the candidate ID. A partial rerun without the
same-attempt upstream producer fails closed. Operators use **Re-run all jobs**
as the only supported recovery procedure; the full rerun creates a coherent
new candidate ID and artifact namespace.

The publish summary covers resolved deployable leaves only. Finalization
allows exactly the standard OCI image-index media type
`application/vnd.oci.image.index.v1+json` or Docker manifest-list media type
`application/vnd.docker.distribution.manifest.list.v2+json`. Each manifest
must contain exactly `linux/amd64` and `linux/arm64`, and its descriptor child
digests must equal the immutable digests in the two native evidence artifacts.
It also validates the metadata descriptor's digest/media type/size, requires
the raw manifest media type to match that descriptor, hashes the raw
`repository@digest` response, and requires the mutable deploy tag to return
the same bytes before writing the summary. The optional terminal lock path is:

```text
artifacts/kolla-ansible-image-lock-<stream>.yml
```

Only `deployment/all` may produce that generic candidate lock. A core,
Keystone, partial deployment, incomplete evidence set, or invalid publish
summary cannot produce one.

`publish-<stream>-<candidate-id>` is uploaded as the complete candidate
artifact before any stream alias changes. A partial stream-alias failure fails
the workflow but cannot invalidate that candidate lock. Recovery uses
**Re-run all jobs**, creating a new candidate ID; aliases are non-transactional
convenience references and are never lock inputs.

## Kolla-Ansible multi-architecture consumption

Operations set registry and stream defaults in `globals.yml` without an
architecture suffix:

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

The generated candidate lock is supplied alongside `globals.yml` as an
operations-managed `globals.d` file or explicit extra-vars file:

For candidate ID `123456789-1`, the corresponding image references are:

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

Before deployment, openstack-infra-ops resolves every deploy_ref, compares its
manifest bytes and digest with manifest_digest and immutable_ref, and only then
passes the root-level variables to Kolla-Ansible. The pinned Kolla-Ansible
releases do not enforce digest identity themselves; a successful extra-vars
load is not a substitute for this verification.

Operators do not select `-amd64` or `-arm64` image tags in Kolla-Ansible.
Docker or Podman automatically chooses the matching child from the
multi-architecture manifest on both homogeneous and mixed-architecture
clusters.

## Manual GitHub and GHCR prerequisites

An organization administrator must complete these actions before the first
separately approved real publish:

1. Create or verify native AMD64 and ARM64 self-hosted runner groups with the
   `x64`/`ARM64` and `kolla-build` labels, Docker, Buildx, and package/registry
   network access. Hard-cap the eligible pool at a maximum of four online
   runners; the workflow uses `max-parallel: 2` and excess jobs queue. Require
   Actions Runner 2.327.1 or newer, at least 150 GiB free Docker storage, and a
   300 GB operational target.
2. Create the protected `ghcr-publish` environment, configure required
   reviewers, and set its branch/tag restrictions to admit only the reviewed
   protected ref `main`, not arbitrary branches or tags.
3. Define `ALLOW_GHCR_PUBLISH`, `ALLOW_GHCR_FULL_CORE_PUBLISH`, and
   `ALLOW_GHCR_DEPLOYMENT_PUBLISH`; set only the exact approved scope to
   `true` during a controlled publication window.
4. Permit `packages: write` only for the dispatched workflow's two
   package-writing `GITHUB_TOKEN` jobs and verify organization Actions/package
   policy. CI starts a separate `workflow_dispatch` with `gh workflow run
   publish.yml --ref main --field stream=2025.1-rocky-9 --field profile=core
   --field image=keystone --field dry_run=true` (or API `ref: main`) using a
   repository-scoped GitHub App installation token, or equivalent short-lived
   credential, with `Actions: write` and no package-write permission. CI never
   supplies candidate ID.
5. After first publication, set package visibility, repository linkage, and
   organization package/Actions permissions as required.
6. If packages remain private, provision a read-only `read:packages` service
   account for Kolla hosts and keep its credential outside this repository.
7. Define retention, vulnerability scanning, and package cleanup policy.
8. Copy the exact count-bearing approval phrase from the dry-run plan, and
   leave `dry_run: true` until capacity, checks, settings, and reviewers are
   ready.

None of these manual prerequisites were performed by this implementation. It
did not dispatch a workflow or change runners, environments, variables,
packages, visibility, repository linkage, or credentials.

## Handoff and secret boundary

The validated publish summary and, only for `deployment/all`, the generic
candidate lock are this repository's terminal outputs. `openstack-infra-ops`
or another dedicated external deployment/promotion system reviews and copies
the lock, creates environment-specific locks and pointers, performs
matching-OS deployment smoke, and owns promotion, site deployment, and
rollback.

Registry credentials, OpenStack credentials, Ceph keys, private CAs, and
site-specific configuration remain in external secret/configuration domains
and are never embedded in images or generated candidate locks.
