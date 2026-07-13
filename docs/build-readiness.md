# Real build readiness

This document defines the prerequisites and evidence for an approved
`dry_run: false` publication. It does not authorize a publish. Complete the
manual GitHub/GHCR checklist in [publish.md](publish.md) before the first real
run.

## Native runner pool

`build-native` is one static two-entry matrix with `max-parallel: 2`:

```text
self-hosted, linux, x64, kolla-build
self-hosted, linux, ARM64, kolla-build
```

The first label set must report `runner_machine: x86_64`; the second must
report `runner_machine: aarch64`. The workflow rejects a mislabeled machine
before registry login. The total pool eligible for the `kolla-build` label is
hard-capped at a maximum of four online native runners across AMD64 and ARM64.
One workflow run still limits its two architecture legs with
`max-parallel: 2`; when cross-stream demand exceeds the physical pool, excess
jobs queue. GitHub Actions does not provide a repository counting semaphore in
this workflow, so the physical eligible-pool cap is the concurrency ceiling.
Each self-hosted runner must use Actions Runner 2.327.1 or newer because the
selected Node 24 action releases require that runner minimum.

Each runner needs:

- native Linux for its advertised architecture;
- Docker Engine and Buildx;
- system Python 3 for pre-login checks; the workflow then selects Python 3.12;
- network access to the pinned Python package source and GHCR;
- a directly created virtual environment containing the matrix-pinned Kolla
  package and exact Docker SDK pin `docker==7.1.0`, plus the workflow's pip
  cache;
- ephemeral Docker storage suitable for a complete Kolla profile and cache.

The workflow resolves Docker's root with `DockerRootDir` and checks it with
`df -Pk`. At least 150 GiB must be free or the build fails; 300 GB is the
operational target for a full profile and reusable cache.

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
references, structured command arrays, evidence paths, summary/lock paths, and
exact count-bearing approval phrase. The workflow installs the matrix-pinned
Kolla version and `docker==7.1.0`, then imports the SDK, checks its version,
and exercises `kolla-build --version` before registry login. It does not use a
hard-coded Kolla version or moving branch.

For a full profile, each native matrix leg executes one dependency-aware
`kolla-build` invocation per architecture. Kolla builds the required parents
and selected leaves in dependency order and uses bounded internal threads.
Profile build groups remain catalog and reporting units; they are not workflow
job fan-out.

## Native image evidence

For a real candidate ID `123456789-1`, the planned child references are:

```text
ghcr.io/supergate-hub/kolla-container-images/nova-compute:2025.1-rocky-9-candidate-123456789-1-amd64
ghcr.io/supergate-hub/kolla-container-images/nova-compute:2025.1-rocky-9-candidate-123456789-1-arm64
```

The two matrix legs upload exact `native-<arch>-<candidate-id>` artifacts:
`native-amd64-<candidate-id>` and `native-arm64-<candidate-id>`.
For every selected deployable leaf, each evidence document records the stream,
Kolla pin, `runner_machine`, native platform, architecture tag, child digest,
immutable reference, and deterministic smoke result.
Required parent refs and digests are recorded separately; the deterministic
container-start smoke applies to deployable leaves.

Kolla summaries and logs are preserved separately as
`native-diagnostics-amd64-<candidate-id>` and
`native-diagnostics-arm64-<candidate-id>`. This keeps each
`native-<arch>-<candidate-id>` artifact limited to its exact evidence JSON
while retaining diagnostics even when a native build step fails.

Before evidence is accepted, the native job:

1. normalizes Docker to a verified local Unix socket, requires a Linux server
   architecture matching the runner, and checks the runner machine against
   `x86_64` or `aarch64`;
2. deletes the planned summary before Kolla runs and validates the current
   Kolla summary against the frozen plan before any remote tag is accepted;
3. inspects the remote architecture-tag descriptor and requires the planned
   `linux/amd64` or `linux/arm64` platform;
4. constructs the immutable `repository@sha256:<child-digest>` reference;
5. performs an immutable pull for that digest and inspects the local OCI
   platform;
6. starts each leaf with its entrypoint overridden to `/bin/true` on the
   matching native runner.

Both package-writing jobs use fresh job-scoped `DOCKER_CONFIG` directories
below `RUNNER_TEMP`. An `always()` cleanup removes only `config.json` after the
ordinary steps, without preempting Buildx post-job cleanup. Forced termination
can bypass cleanup, so persistent runners still require cleanup or reimaging.

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
candidate ID. A partial rerun without its upstream producer fails closed;
operators use **Re-run all jobs** as the supported recovery procedure, which
creates a new coherent attempt. A partial stream-alias failure fails the
workflow but cannot invalidate the already uploaded candidate lock, and its
retry receives a new candidate ID.

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
   plan.
2. Complete all eight manual runner, protected-environment, variable,
   token-permission, package, credential, and policy prerequisites in
   [publish.md](publish.md).
3. Use the narrow `core/keystone` scope for the first separately approved real
   publish.
4. Require both native evidence artifacts and the exact two-platform
   multi-architecture manifest checks before accepting the Keystone result.
5. Only after that evidence is accepted, separately authorize a full
   `2025.1-rocky-9` `deployment/all` candidate and hand its generic lock to
   operations for external deployment smoke.

Registry credentials, OpenStack credentials, Ceph keys, private CAs,
kubeconfigs, and site-specific configuration stay outside images and generated
candidate locks. No runner, GitHub, GHCR, credential, publish, or deployment
change is performed by this readiness document.
