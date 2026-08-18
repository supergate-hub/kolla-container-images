# Real build readiness

This document defines evidence required for `operation=publish`. It does not
authorize a publish. Complete the GitHub/GHCR checklist in
[publish.md](publish.md) first. Only protected `2025-1`, `2025-2`, and `2026-1`
release branches may publish; `main` is aggregate validation and plan only.

## Native runner contract

| Architecture | Standard hosted runner | Required machine/platform |
| --- | --- | --- |
| AMD64 | `ubuntu-24.04` | `x86_64`, `linux/amd64` |
| ARM64 | `ubuntu-24.04-arm` | `aarch64`, `linux/arm64` |

Larger and privately managed runner fleets are outside this design. Every
parent and leaf matrix uses `max-parallel: 4`. A fresh runner requires local
Linux Docker, Buildx, Python 3.12, and network access to the pinned source
repositories, configured OS registry, and GHCR.

After `docker system prune -af --volumes`, a unit must have at least 8 GiB free
in Docker's filesystem. Sampling during and after the build must never observe
less than 2 GiB. Evidence records the initial, post-cleanup, post-ancestor,
minimum-build, and post-build measurements. Standard runners have about 14 GB,
so a profile is sharded to one target per job.

The hosted approach remains a feasibility gate until Keystone completes these
eight native jobs:

```text
AMD64: base -> openstack-base -> keystone-base -> keystone
ARM64: base -> openstack-base -> keystone-base -> keystone
```

QEMU may assist debugging but is not readiness evidence for ARM64 publication,
image smoke, or deployment smoke.

## Frozen plan contract

Use `operation=plan` in Actions or the local read-only planner before a
publish. Tests and offline review supply a checked base-manifest fixture; a
normal plan resolves the configured base tag:

```bash
python3 scripts/plan-publish.py \
  --stream 2025.1-rocky-10.2-20.5.0 \
  --profile core \
  --image keystone \
  --candidate-id local-dry-run \
  --dry-run
```

The workflow candidate ID is `github.run_id`-`github.run_attempt`; users do not
input it. The plan freezes:

- release branch and pinned OpenStack Releases, Kolla, and Kolla-Ansible
  commits;
- the complete OpenStack source-set and canonical digest;
- generated `kolla-build.conf` and template-override content/digests;
- the configured OS tag, OCI index digest, and exact AMD64/ARM64 child
  descriptors;
- selected leaves, dependency tiers, ancestor chains, semantic/revision refs,
  exact unit matrices, evidence paths, and lock eligibility.

The checkout is detached at every exact commit. The Python build engine is
installed from `config/build-engine-requirements.lock` with exact versions,
artifact hashes, and `--require-hashes`; its complete installed distribution
set and lock digest are verified. Kolla is then installed from its local
source checkout with no dependency resolution or network access, and its
version and source provenance are verified. Kolla-Ansible is not installed
for the image build, but its exact commit is carried as downstream provenance.
The workflow does not install Kolla from PyPI by version and never builds a
moving branch.

OpenStack has no single release-wide build commit. The source-set contains an
exact `build_commit` for every required service, parent, addition, Horizon
plugin, and `requirements`. `stable/YYYY.N` is only tracking metadata. Frozen
Kolla source configuration uses the exact commits, and requirements/Kolla
Toolbox upper constraints use the same pinned requirements commit. Missing or
moving source references fail closed. Active source-set schema v3 also records
the exact Kolla/Kolla-Ansible pins and the Kolla `sources.py` and normalized
closure digests for each compatible toolchain; a new toolchain requires a new
append-only source-set revision. CI and publish planning compare the toolchain
pins against an exact checkout of the matrix-pinned OpenStack Releases commit.
Direct Dockerfile inputs in the reviewed
closure, including `ovn-ctl` and the Epoxy MariaDB `clustercheck`, use
commit-addressed URLs and are checked against source-set SHA-256 values during
the build. Toolbox constraint bytes are checked the same way.

Each native unit fetches only the projects required by its selected target.
The fetch excludes remote tags and exposes only the frozen commit plus its
recorded release tag. The hash-locked PBR installation derives the exact
package version from that closed graph. Tracked files and a matching
`PKG-INFO` are then exported to a sorted, metadata-normalized local archive
without `.git`; Kolla consumes the archive through `--locals-base`. Archive
bytes are regenerated and compared before registry login.

The matrix stores a base tag, not a digest. Plan generation resolves it exactly
once. Each native unit pulls the frozen platform child digest, verifies the
digest/platform, retags it locally to the configured base reference, and runs
Kolla with Kolla's upstream `--nopull` option. A tag move after plan generation
cannot alter that run. A later plan may intentionally create a new revision from a newly
resolved base. DNF/APT repository snapshots are excluded, so package-level
bit-for-bit rebuild reproducibility is not claimed.

## Build-unit and native evidence

The plan creates parent dependency tiers 0, 1, and 2, leaf stage 0, and
optional leaf stage 1. Each unit has one anchored target and its exact ancestor
chain. The frozen Kolla command uses `--threads 1`, `--push-threads 1`,
`--nopull`, the frozen config files, and exactly one `--skip-existing`. The
installed exact Kolla parser must accept the full argv with `pull=False` before
registry login.
Before invoking Kolla, the unit pulls each ancestor by its immutable evidence
digest, verifies and retags it locally, and proves that the selected target's
revision tag is absent. This makes `--skip-existing` skip only proven
ancestors. `--skip-parents` remains forbidden because it can skip the target
itself.

```text
parent tier 0 -> parent tier 1 -> parent tier 2
              -> leaf stage 0 -> optional leaf stage 1
              -> native evidence aggregation
```

There is no parent-index artifact. A dependent job pulls ancestor immutable
refs from raw unit evidence, verifies native platform and digest, and retags
them locally. Kolla's current summary must contain exactly the target in
`built` and the planned ancestors in `skipped`.

For candidate `123456789-1`, a sample native revision pair is:

```text
ghcr.io/supergate-hub/kolla-container-images/nova-compute:2025.1-rocky-10.2-20.5.0-rev-123456789-1-amd64
ghcr.io/supergate-hub/kolla-container-images/nova-compute:2025.1-rocky-10.2-20.5.0-rev-123456789-1-arm64
```

Every parent and leaf uploads
`unit-evidence-<arch>-<kind>-<target>-<candidate-id>`. Schema-v3 unit evidence
records candidate and unit identity, Kolla/Kolla-Ansible pins, full OpenStack
source provenance, frozen base index/child digests, runner machine/platform,
ancestor refs/digests, exact Kolla summary, disk measurements, target digest
and immutable reference, and leaf smoke.

Before evidence is accepted, the unit:

1. verifies the native runner and local Linux Docker Unix socket;
2. prunes Docker and passes the 8 GiB preflight;
3. installs the exact source checkouts, materializes hash-verified frozen
   Kolla source configuration, and verifies the unit's deterministic local
   source archives;
4. pulls/verifies/retags the frozen base child and every ancestor;
5. executes the one-target command while sampling disk;
6. checks exact `built`/`skipped` sets and the 2 GiB minimum;
7. verifies the pushed target descriptor, immutable digest, and native
   platform; and
8. for a leaf, starts the immutable image with `/bin/true` as the overridden
   entrypoint.

After every planned unit succeeds, aggregation validates the exact closure and
creates `native-amd64-<candidate-id>` and
`native-arm64-<candidate-id>`. Native evidence preserves the same base and
source-set provenance; any source/config/base/child digest substitution fails.

Plan, unit/native, and terminal success artifacts contain JSON/YAML evidence,
not Docker layers, image tar files, Docker directories, pip caches, or build
caches. Successful evidence is retained seven days; failure diagnostics are
retained one day. A failed unit uploads its logs as the matching
`unit-diagnostics-<unit-id>-<candidate-id>` artifact. Job-scoped
`DOCKER_CONFIG` state is removed in cleanup.

## Manifest, summary, and lock evidence

Finalization downloads the exact plan and both native artifacts. It creates a
revision multi-architecture manifest from immutable child digest refs, not
from mutable tags. For candidate `123456789-1`:

```text
revision_ref:
ghcr.io/supergate-hub/kolla-container-images/nova-compute:2025.1-rocky-10.2-20.5.0-rev-123456789-1

semantic_ref:
ghcr.io/supergate-hub/kolla-container-images/nova-compute:2025.1-rocky-10.2-20.5.0
```

The raw manifest must be exactly an OCI image index
`application/vnd.oci.image.index.v1+json` or Docker manifest list
`application/vnd.docker.distribution.manifest.list.v2+json`, with exactly two
descriptors and the exact platforms `linux/amd64` and `linux/arm64`. Child
digests must match native evidence. The validator also checks descriptor
digest/media type/size and hashes the raw immutable manifest bytes.

The schema-v3 publish summary repeats the frozen release metadata, Kolla and
Kolla-Ansible commits, source-set provenance, generated config digests, base
index/child digests, scope, semantic/revision refs, manifest digest, immutable
ref, and native child records. Only a complete `deployment/all` summary may
produce `artifacts/kolla-ansible-image-lock-<stream>.yml`.

The terminal artifact `publish-<stream>-<candidate-id>` contains the validated
summary, raw manifests under `artifacts/manifests/`, and the eligible generic
candidate lock. It is uploaded before the semantic alias is moved. A failed
semantic write therefore cannot invalidate the immutable revision lock.

Lock schema v3 preserves both refs but root Kolla-Ansible variables use the
revision ref:

```yaml
_kolla_candidate_lock:
  schema_version: 3
  stream: "2025.1-rocky-10.2-20.5.0"
  base:
    requested_ref: "quay.io/rockylinux/rockylinux:10.2"
    index_digest: "sha256:<base-index-digest>"
  openstack_sources:
    source_set: {id: "epoxy-20260813-r1", projects: "<full pinned mapping>"}
    canonical_digest: "sha256:<source-set-digest>"
  images:
    nova-compute:
      semantic_ref: "ghcr.io/supergate-hub/kolla-container-images/nova-compute:2025.1-rocky-10.2-20.5.0"
      revision_ref: "ghcr.io/supergate-hub/kolla-container-images/nova-compute:2025.1-rocky-10.2-20.5.0-rev-123456789-1"
      manifest_digest: "sha256:<multi-arch-manifest-digest>"
      immutable_ref: "ghcr.io/supergate-hub/kolla-container-images/nova-compute@sha256:<multi-arch-manifest-digest>"
nova_compute_image_full: "ghcr.io/supergate-hub/kolla-container-images/nova-compute:2025.1-rocky-10.2-20.5.0-rev-123456789-1"
```

Candidate ID joins the plan and candidate artifact; candidate-qualified image
tags are not published. The semantic contract is
`{release}-{distro}-{os_version}-{kolla_ansible_version}` and native revision
tags add `-rev-<run_id>-<run_attempt>-<arch>`. Existing major/codename tags are
retained but not updated or aliased to the exact-version images.

Do not use **Re-run failed jobs**. Use **Re-run all jobs** so a new candidate ID
and revision rebuild the entire closure.

## Image smoke versus deployment smoke

Native evidence belongs here and is keyed by
`stream × architecture × build unit`. It proves the immutable image can be
pulled and, for leaves, executed natively. Matching-OS Kolla-Ansible service
evidence is keyed by `stream × architecture` and remains external.
Environment-specific deployment-smoke evidence remains external.
`openstack-infra-ops` owns service checks for Keystone, Nova, Cinder, Manila,
Octavia, observability, promotion, deployment, and rollback.
Before deployment it verifies each revision and immutable ref against the
manifest bytes and digest recorded in the candidate lock.

## First-publish readiness sequence

1. Validate schema v4 and inspect `operation=plan` for every release-local
   stream/scope. Confirm the full OpenStack source-set/config hashes and frozen
   base index/AMD64/ARM64 digests.
2. Verify protected release branches, required validation,
   `ghcr-publish` reviewers/branch restrictions, and read-default Actions
   permissions. `main` must remain unable to publish.
3. Publish only `2025-1 / 2025.1-rocky-10.2-20.5.0 / keystone` first.
4. Require all eight native units, 8 GiB preflight, 2 GiB observed minimum,
   exact source/base provenance, both aggregate evidence files, the revision
   two-platform manifest, summary, and semantic digest verification.
5. Confirm new GHCR packages are linked to this repository, explicitly Public,
   and anonymously inspectable/pullable with an empty Docker config.
6. Expand independently to core and deployment. After the deployment summary
   and lock pass, hand the generic lock to `openstack-infra-ops`.

This document performs no runner, GitHub, GHCR, credential, package,
deployment, or environment change. Secrets, private CAs, Ceph keys,
kubeconfigs, and site configuration remain outside images and candidate locks.
