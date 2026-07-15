# Real build readiness

This document defines the prerequisites and evidence for an approved
`dry_run: false` publication. It does not authorize a publish. Complete the
manual GitHub/GHCR checklist in [publish.md](publish.md) before the first real
run.

## Standard hosted runner contract

The source repository is Public, and native builds use only GitHub's standard
hosted runners:

| Architecture | Runner | Required machine and platform |
| --- | --- | --- |
| AMD64 | `ubuntu-24.04` | `x86_64`, `linux/amd64` |
| ARM64 | `ubuntu-24.04-arm` | `aarch64`, `linux/arm64` |

One Public repository is sufficient; splitting architectures into separate
repositories adds no billing advantage. Under the current GitHub policy,
standard hosted runner usage for a Public repository is free without billed
minute limits, while larger runners are always billed. Public GHCR package
storage and bandwidth are also currently free. These are current policy
assumptions, not permanent guarantees, and must be reviewed if GitHub changes
its billing terms.

Larger runners and privately managed runner fleets are not part of this
workflow. Every parent-tier and leaf-stage matrix uses `max-parallel: 4`. Each fresh VM
must provide local Linux Docker, Buildx, Python 3, network access to the pinned
Python packages and GHCR, and the advertised native architecture. The workflow
selects Python 3.12 and installs the matrix-pinned Kolla package with
`docker==7.1.0` using `--no-cache-dir`.

The standard runner has 14 GB of SSD storage, so a complete profile is never
built in one job. After `docker system prune -af --volumes`, each unit must
have at least 8 GiB free in Docker's filesystem. The runner samples free space
during the build and rejects evidence when the observed minimum during or
immediately after build is below 2 GiB. Evidence records the initial,
post-cleanup, post-ancestor, minimum-build, and post-build measurements.

These checks make disk failure explicit; they do not prove in advance that
every Kolla target fits. The hosted-only approach remains a feasibility gate
until the Keystone canary completes all eight fresh jobs:

```text
AMD64: base -> openstack-base -> keystone-base -> keystone
ARM64: base -> openstack-base -> keystone-base -> keystone
```

QEMU can help local debugging, but QEMU output is not readiness evidence for
ARM64 publication, image smoke, or deployment smoke.

## Frozen plan and build unit

Render and inspect a plan locally before asking for publication approval:

```bash
python3 scripts/plan-publish.py \
  --stream 2025.1-rocky-9 \
  --profile core \
  --image keystone \
  --candidate-id local-dry-run \
  --dry-run
```

The workflow derives candidate ID from `github.run_id` and
`github.run_attempt`; local read-only planning uses `local-dry-run`. The plan
freezes the stream pin, resolved leaf set, parent chains, native
references, exact build-unit matrices, evidence paths, summary/lock paths, and
exact count-bearing approval phrase. The workflow installs the matrix-pinned
Kolla version and `docker==7.1.0`, then imports the SDK, checks its version,
and exercises `kolla-build --version` before registry login. It does not use a
hard-coded Kolla version or moving branch.

The plan places parents in dependency tiers 0, 1, and 2, then creates leaf
stage 0 and optional leaf stage 1. Stage 1 is normally empty; the deployment
profile uses it for the selected-leaf dependency
`ovn-sb-db-server -> ovn-sb-db-relay`. Each unit has one anchored target and an
exact ancestor chain. It pulls the preceding raw unit evidence by immutable
digest, validates the platform, applies only the planned candidate tag locally,
and runs `kolla-build` with `--skip-existing`, `--threads 1`, and
`--push-threads 1`. The current Kolla summary must contain exactly the target
in `built` and exactly the ancestor chain in `skipped`.

```text
parent tier 0 -> parent tier 1 -> parent tier 2
              -> leaf stage 0 -> optional leaf stage 1
              -> aggregate native evidence
              -> manifests/summary/generic candidate lock
              -> hand off to openstack-infra-ops
```

There is no parent-index artifact. Downstream units consume immutable digests
from raw `unit-evidence` JSON, and native aggregation verifies the complete
frozen-plan closure before finalization.

For `2025.1-rocky-9 deployment/all`, the plan contains 16 parents and 63 leaves
per architecture: 32 parent jobs, 124 leaf-stage-0 jobs, and 2
leaf-stage-1 jobs, or 158 native build jobs.

## Native image evidence

For a real candidate ID `123456789-1`, the planned child references are:

```text
ghcr.io/supergate-hub/kolla-container-images/nova-compute:2025.1-rocky-9-candidate-123456789-1-amd64
ghcr.io/supergate-hub/kolla-container-images/nova-compute:2025.1-rocky-9-candidate-123456789-1-arm64
```

Every parent and leaf job uploads one
`unit-evidence-<arch>-<kind>-<target>-<candidate-id>` JSON artifact. Parent
and leaf jobs pass those raw JSON documents forward directly. After every
planned unit succeeds, native aggregation checks the exact all-unit closure
and creates the exact `native-amd64-<candidate-id>` and
`native-arm64-<candidate-id>` JSON artifacts consumed by finalization. Their
shared pattern is `native-<arch>-<candidate-id>`.

Unit evidence records the candidate and Kolla pin, frozen unit identity,
runner machine and platform, target digest and immutable reference, every
ancestor ref and digest, exact `built`/`skipped` summary, all disk
measurements, and the leaf smoke result. Before accepting it, the unit job:

1. verifies the hosted native machine and local Unix Docker socket, prunes
   Docker, and passes the 8 GiB preflight;
2. pulls every planned ancestor by immutable digest, verifies its local native
   platform and digest, and applies its candidate tag locally;
3. executes the one-target frozen command while sampling disk space;
4. validates exact `built` and `skipped` summary sets and the 2 GiB minimum;
5. verifies the pushed target descriptor, immutable digest, and native
   platform;
6. for a leaf, starts that immutable image with its entrypoint overridden to
   `/bin/true`.

The publish plan, successful unit evidence, aggregate native evidence, and
terminal publish artifact contain JSON evidence only and are retained for
seven days. A failed unit may upload a separate text diagnostic retained for
one day. No job uploads a Docker layer, image tar file, Docker directory, pip
cache, or Docker build cache as an artifact. Each package-writing unit uses a
fresh job-scoped `DOCKER_CONFIG` below `RUNNER_TEMP` and removes its
`config.json` in an `always()` cleanup step.

This is image evidence keyed by `stream × architecture × leaf`. It proves that
the immutable child can be pulled and executed natively, not that an OpenStack
service deployment works.

## Multi-architecture manifest, summary, and lock evidence

`finalize-publish` downloads `publish-plan-<candidate-id>`,
`native-amd64-<candidate-id>`, and `native-arm64-<candidate-id>` by their exact
artifact names. It creates each deployable
architecture-neutral tag from the two immutable child digest references, not
from mutable architecture tags.

The raw manifest media type must be exactly one of the standard
`application/vnd.oci.image.index.v1+json` OCI image index or
`application/vnd.docker.distribution.manifest.list.v2+json` Docker manifest
list types. It must contain exactly two descriptors, contain no unexpected
annotations, and have the exact platform set `linux/amd64` and `linux/arm64`.
Its child digests must match the recorded native evidence. Finalization
validates the metadata descriptor's digest, media type, and size; requires the
raw media type to match that descriptor; fetches `repository@digest`; hashes
those exact raw bytes; and requires the mutable deploy tag to return identical
bytes before recording the digest for every selected leaf in
`artifacts/publish-summary-<stream>.json`.

The terminal upload is `publish-<stream>-<candidate-id>`, containing the
validated summary, raw multi-architecture manifest evidence under
`artifacts/manifests/`, and a candidate lock only for a complete
`deployment/all` scope. The workflow uploads this candidate artifact before it
changes any stream alias. Generate that lock from a validated full summary
with the same expected identity:

```bash
python3 scripts/generate-lock.py \
  --publish-summary artifacts/publish-summary-2025.1-rocky-9.json \
  --stream 2025.1-rocky-9 \
  --profile deployment \
  --candidate-id 123456789-1 \
  --output artifacts/kolla-ansible-image-lock-2025.1-rocky-9.yml
```

For that candidate, the architecture-neutral ref used by both the lock metadata
and root variable is:

```yaml
_kolla_candidate_lock:
  images:
    "nova-compute":
      deploy_ref: "ghcr.io/supergate-hub/kolla-container-images/nova-compute:2025.1-rocky-9-candidate-123456789-1"
      manifest_digest: "sha256:<multi-arch-manifest-digest>"
      immutable_ref: "ghcr.io/supergate-hub/kolla-container-images/nova-compute@sha256:<multi-arch-manifest-digest>"
nova_compute_image_full: "ghcr.io/supergate-hub/kolla-container-images/nova-compute:2025.1-rocky-9-candidate-123456789-1"
```

The stable stream alias
`ghcr.io/supergate-hub/kolla-container-images/nova-compute:2025.1-rocky-9`
is convenience-only and never a lock input. All upload/download names contain
candidate ID. **Re-run failed jobs is forbidden** because the incremented run
attempt would not have a complete same-candidate upstream artifact set.
Operators recover only with **Re-run all jobs**, which gives every unit a new
candidate ID and rebuilds the entire dependency closure. A partial stream-alias
failure cannot invalidate the already uploaded candidate lock; its recovery is
also a full rerun under a new candidate ID.

The lock contains tag-only architecture-neutral *_image_full variables for
Kolla-Ansible plus a reserved _kolla_candidate_lock mapping. For every image,
that mapping records deploy_ref, manifest_digest, immutable_ref, and the
associated Kolla-Ansible variables. It is a generic, digest-bound candidate
for handoff to `openstack-infra-ops`. Before deployment, that repository must
verify that each deploy tag still resolves to the recorded digest and bytes.
It is not an environment lock, promotion pointer, or deployment action.

## Image smoke versus deployment smoke

Native image evidence belongs here and is keyed by
`stream × architecture × leaf`. Matching-OS Kolla-Ansible service evidence is
keyed by `stream × architecture`. Environment-specific deployment-smoke
evidence remains external. `openstack-infra-ops` or a dedicated native
deployment harness owns prechecks and smoke for Keystone, Nova, Cinder,
Manila, Octavia, Prometheus/Grafana, and OpenSearch ingestion.

Compatibility smoke must run on the matching base OS and native architecture,
but it does not create a standing compatibility cluster. Neither image smoke
nor deployment smoke may substitute QEMU-only ARM64 output for native
evidence.

## First-publish readiness sequence

1. Run `python3 scripts/validate-config.py` and inspect the required dry-run
   plan. A feature branch may run `dry_run: true`; it cannot mutate GHCR.
2. Complete the pre-canary public-repository, protected-main ruleset,
   `ghcr-publish` required-reviewer/main-only restriction, repository-variable,
   and read-default workflow permission steps in [publish.md](publish.md).
   The code-level protected-main guard blocks a real publish until `main` is
   actually protected.
3. Use the narrow `core/keystone` scope for the first separately approved real
   publish from protected `main`, with the exact approval phrase and only
   `ALLOW_GHCR_PUBLISH=true`. It must use the two standard hosted labels and
   produce exactly eight parent/leaf build units. Before the first push, verify
   that no same-name pre-existing GHCR package is unlinked from this repository.
   Complete the environment approval within 48 hours of plan creation; otherwise
   cancel it and start a fresh workflow run/candidate instead of approving a
   nearly expired seven-day plan.
4. Require every unit to pass the 8 GiB preflight and observed 2 GiB minimum,
   then require both native evidence artifacts, the publish summary, and the
   exact two-platform multi-architecture manifest.
5. The GHCR package may not exist before that first push. After the canary,
   verify each new package is linked to this repository, inspect it for
   secrets/site-specific content, explicitly change visibility to **Public**
   (treated as irreversible), and verify an anonymous manifest inspection and
   pull after `docker logout ghcr.io` or with an empty Docker config. Only then
   accept the Keystone canary. No actual canary is claimed by this document.
6. Only after that evidence is accepted, separately authorize a full
   `2025.1-rocky-9` `deployment/all` candidate. Repeat repository-link,
   irreversible Public-visibility, and anonymous-pull verification for every
   package first created by that run. The workflow uses GitHub's documented
   `GITHUB_TOKEN` inheritance default, but acceptance depends on observed
   package visibility rather than that assumption. Only after every deployable
   leaf package is Public, hand the generic lock to operations for external
   deployment smoke.

Registry credentials, OpenStack credentials, Ceph keys, private CAs,
kubeconfigs, and site-specific configuration stay outside images and generated
candidate locks. No runner, GitHub, GHCR, credential, publish, or deployment
change is performed by this readiness document.
