# kolla-container-images

This repository builds native AMD64 and ARM64 Kolla images, publishes them to
`ghcr.io/supergate-hub/kolla-container-images`, creates two-platform manifests,
and emits provenance-rich publish summaries and generic candidate locks for
Kolla-Ansible.

The pipeline policy requires every stream to be built and image-smoked on
native ARM64 CI as well as native AMD64 CI.

## Responsibility boundary

```text
build -> publish revision architecture images -> create revision manifest
      -> publish summary -> generic candidate lock artifact
      -> update semantic alias -> hand off to openstack-infra-ops
```

The handoff is the terminal boundary. `openstack-infra-ops` owns environment
selection, environment-specific locks and pointers, deployment validation,
promotion, deployment orchestration, rollback, Ceph, credentials, and
site-specific configuration. None of that state or automation belongs here.

## Branch and configuration model

`config/build-matrix.json` uses schema v4. `main` is the aggregate catalog and
cannot publish. Protected release branches contain the same workflows,
scripts, tests, and docs as `main`, but only their release-owned matrix rows and
OpenStack source-set files:

```text
main
├── .github/workflows/, scripts/, tests/, docs/
└── config/
    ├── build-matrix.json               # every supported release and stream
    ├── openstack-sources/
    │   ├── epoxy-*.json
    │   ├── flamingo-*.json
    │   └── gazpacho-*.json
    └── profiles/

2025-1
├── same common code
└── config/
    ├── build-matrix.json               # 2025.1 only
    ├── openstack-sources/epoxy-*.json
    └── profiles/

2025-2                                      # 2025.2 + Flamingo source set only
2026-1                                      # 2026.1 + Gazpacho source set only
```

The branch name is derived from the OpenStack release (`2025.1` -> `2025-1`)
and is not duplicated in the matrix. Validation rejects mixed-release branch
catalogs, unknown references, unused toolchains or bases, duplicate
release/toolchain/base combinations, and stream IDs that differ from their
rendered semantic tag.

Schema v4 separates four concerns:

- `releases` maps an OpenStack release and series to an immutable source-set
  revision.
- `toolchains` is keyed by the common Kolla/Kolla-Ansible version. Their
  repositories and exact 40-character commits remain separate and must match
  the pinned OpenStack Releases metadata.
- `bases` records only `distro`, exact `os_version`, image repository, and
  image tag. Digests are deliberately absent from raw configuration.
- `streams` joins one release, toolchain, and base. It does not repeat Kolla
  versions, base tags, tag tokens, or digests.

## Supported streams

The aggregate `main` catalog contains exactly these nine active streams:

| Stream ID / semantic tag | Release branch | Toolchain | Configured base | Deployment leaves |
| --- | --- | --- | --- | ---: |
| `2025.1-rocky-9.8-20.4.0` | `2025-1` | Kolla / Kolla-Ansible `20.4.0` | Rocky `9.8` | 63 |
| `2025.1-rocky-10.2-20.4.0` | `2025-1` | Kolla / Kolla-Ansible `20.4.0` | Rocky `10.2` | 63 |
| `2025.1-ubuntu-24.04-20.4.0` | `2025-1` | Kolla / Kolla-Ansible `20.4.0` | Ubuntu `24.04` | 64 |
| `2025.1-rocky-10.2-20.5.0` | `2025-1` | Kolla / Kolla-Ansible `20.5.0` | Rocky `10.2` | 63 |
| `2025.1-ubuntu-24.04-20.5.0` | `2025-1` | Kolla / Kolla-Ansible `20.5.0` | Ubuntu `24.04` | 64 |
| `2025.2-rocky-10.2-21.1.0` | `2025-2` | Kolla / Kolla-Ansible `21.1.0` | Rocky `10.2` | 63 |
| `2025.2-ubuntu-24.04-21.1.0` | `2025-2` | Kolla / Kolla-Ansible `21.1.0` | Ubuntu `24.04` | 64 |
| `2026.1-rocky-10.2-22.0.0` | `2026-1` | Kolla / Kolla-Ansible `22.0.0` | Rocky `10.2` | 65 |
| `2026.1-ubuntu-24.04-22.0.0` | `2026-1` | Kolla / Kolla-Ansible `22.0.0` | Ubuntu `24.04` | 66 |

The `core` profile resolves to 21 leaves for every stream. The two 2025.1
Rocky 10.2 streams demonstrate why a release cannot own only one toolchain:
20.4.0 and 20.5.0 coexist without overwriting each other. When Rocky 10.3 is
needed, add a new `rocky-10.3` base and new streams; do not mutate the 10.2
identity.

## Pinned OpenStack sources

There is no single commit representing all OpenStack services. Each release
therefore points to a separate source-set under `config/openstack-sources/`.
It covers the source closure used by the reviewed profile: parent images,
services, additions, Horizon plugins, `requirements`, and Kolla Toolbox
constraints. Every Git build input is an exact `build_commit` SHA. Inline
Dockerfile downloads used by the reviewed closure, including `ovn-ctl` and
the Epoxy MariaDB `clustercheck`, are also recorded by commit-addressed URL
and SHA-256 and rendered as checksum-verifying template overrides.

`track_ref` such as `stable/2025.1` records where a snapshot was discovered;
it is metadata and is never passed to Kolla as a build reference.
`nearest_release` is also explanatory metadata. The build uses only
`build_commit`. For each native unit, the required commits are fetched without
remote tags into closed temporary mirrors. The hash-locked PBR installation
derives the version from the frozen release tag and commit ancestry, then the
tracked files and that version are exported as sorted, metadata-normalized,
`.git`-free local archives. Kolla consumes those archives instead of cloning
the upstream repositories. The source-set canonical digest and deterministic
`kolla-build.conf` and template-override digests flow through the frozen plan,
unit/native evidence, publish summary, and lock. A missing pin, moving build
reference, source closure mismatch, or digest mismatch fails closed.

Kolla Toolbox constraints are bound to the exact `requirements` commit and
their recorded SHA-256 is verified in the image build. This keeps those bytes
inside the same source-set provenance contract rather than trusting a moving
redirect or an unverified download.

Source-set files are append-only revisions. Create a new revision when adding
a toolchain or intentionally taking a CVE/bugfix snapshot; do not silently
rewrite an existing revision. Active source-set schema v3 records
`kolla_source_inputs` for every compatible toolchain: both exact Kolla and
Kolla-Ansible pins, the pinned `sources.py` digest, and the normalized source
closure digest. Stream resolution rejects a toolchain that is absent or differs
from this record, so adding a toolchain requires a new source-set revision.

Validation and publish planning also check out the matrix-pinned
`openstack/releases` commit and compare every Kolla/Kolla-Ansible version and
commit against its release deliverables before environment approval.

## Frozen base images

The matrix stores a human-readable base reference such as
`quay.io/rockylinux/rockylinux:10.2`. Plan generation resolves that tag once
and freezes the index digest plus exact `linux/amd64` and `linux/arm64` child
digests. A missing platform fails before any image build.

Each native unit pulls its frozen child digest, verifies the platform and
digest, retags it to the configured local base tag, and invokes Kolla with
Kolla's upstream `--nopull` option. The same run therefore cannot re-resolve the
mutable upstream tag. Before registry login, the installed exact Kolla parser
must accept the complete frozen command with `pull=False`.
A later run may resolve a different digest and will produce a different
revision image with that provenance.

DNF/APT repository snapshots are intentionally out of scope. The plan pins the
container base, OpenStack sources, and Kolla toolchain, but does not yet promise
package-level bit-for-bit rebuilds.

## Image tags and candidate lock

For stream `2025.1-rocky-10.2-20.5.0` and candidate ID `123456789-1`:

```text
semantic alias
2025.1-rocky-10.2-20.5.0

immutable run revision
2025.1-rocky-10.2-20.5.0-rev-123456789-1

native revision children
ghcr.io/supergate-hub/kolla-container-images/nova-compute:2025.1-rocky-10.2-20.5.0-rev-123456789-1-amd64
ghcr.io/supergate-hub/kolla-container-images/nova-compute:2025.1-rocky-10.2-20.5.0-rev-123456789-1-arm64
```

The semantic tag contract is
`{release}-{distro}-{os_version}-{kolla_ansible_version}`. Legacy
`-candidate-...` image tags are removed. Candidate ID remains an internal
identity joining the plan, evidence, summary, and lock, while `-rev-...` names
the immutable published revision.

Default OS aliases are persistent `tag_aliases` entries in
`config/build-matrix.json` (for example, `2025.1-rocky-10.2` points to the
selected 20.5.0 exact stream). The planner includes the configured aliases
automatically; each publish updates them after the immutable revision manifest
has been verified, so operators do not re-enter alias settings per build.

The workflow builds and pushes native revision tags, creates and verifies the
revision multi-architecture manifest, validates and uploads the publish
summary and eligible lock, and only then moves the semantic alias to the
revision digest. A semantic-alias failure cannot invalidate the already
uploaded revision lock. Existing major/codename GHCR tags are not deleted, but
they are no longer updated and are not aliases for the exact-version streams.

Lock schema v3 records both `semantic_ref` and `revision_ref`, the manifest
digest and immutable digest ref, architecture child refs/digests, exact
Kolla/Kolla-Ansible commits, the full OpenStack source-set provenance, and the
resolved base index/child digests. Root-level Kolla-Ansible `*_image_full`
variables use the revision ref, never the mutable semantic alias:

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
      manifest_digest: "sha256:<manifest-digest>"
      immutable_ref: "ghcr.io/supergate-hub/kolla-container-images/nova-compute@sha256:<manifest-digest>"
nova_compute_image_full: "ghcr.io/supergate-hub/kolla-container-images/nova-compute:2025.1-rocky-10.2-20.5.0-rev-123456789-1"
```

Before deployment, `openstack-infra-ops` verifies the revision ref and
immutable ref against the recorded manifest bytes and digest. Kolla-Ansible
selects the correct native child from the multi-architecture manifest; root
variables never carry `-amd64`, `-arm64`, or `@sha256`.

## GitHub Actions

`.github/workflows/publish.yml` exposes exactly three inputs:

| Input | Contract |
| --- | --- |
| `operation` | `plan` (default) or `publish` |
| `stream` | Exact schema-v4 stream ID |
| `scope` | `keystone`, `core`, or `deployment` |

Scopes map to local planner arguments as `keystone -> core/keystone`,
`core -> core/all`, and `deployment -> deployment/all`. The old `profile`,
`image`, `dry_run`, `approval`, and typed approval phrase are not workflow
inputs. Arbitrary profile or single-image planning remains available only in
the local planner CLI.

`operation=plan` creates a frozen plan and Actions summary without registry
mutation, publish summary, or lock. `operation=publish` is rejected from
`main`, tags, feature branches, the wrong release branch, or a disabled stream.
It requires a protected `YYYY-N` branch and the `ghcr-publish` environment
approval. See
[docs/publish.md](docs/publish.md) for the operator contract and
[docs/build-readiness.md](docs/build-readiness.md) for native evidence gates.

## Repository layout

```text
config/build-matrix.json              Aggregate or branch-local schema-v4 catalog
config/openstack-sources/             Immutable OpenStack source-set revisions
config/profiles/                      Reviewed image catalogs and variable mapping
scripts/base_resolution.py            OCI index/child digest resolver and validator
scripts/openstack_source_set.py       Source-set validation and Kolla overrides
scripts/plan-publish.py               Frozen-plan renderer
scripts/run-build-unit.py             One native target build, push, and evidence
scripts/aggregate-native-evidence.py  Exact native closure aggregation
scripts/validate-publish-summary.py   Publish-summary schema-v3 validator
scripts/generate-lock.py              Generic candidate-lock schema-v3 renderer
.github/workflows/validate.yml        Repository validation
.github/workflows/publish.yml         Dispatch-only plan/publish workflow
.github/workflows/build-unit.yml      Reusable one-target native build job
```

## Local validation

```bash
python3 scripts/validate-config.py --branch main
python3 scripts/plan-publish.py \
  --stream 2025.1-rocky-10.2-20.5.0 \
  --profile deployment \
  --candidate-id local-dry-run \
  --dry-run
python3 -m unittest discover -s tests -v
```

On a release-local tree, replace the first command and add the ownership gate:

```bash
python3 scripts/validate-config.py --branch 2025-1
python3 scripts/validate-release-context.py matrix \
  --matrix config/build-matrix.json --branch 2025-1
```

The planner is read-only but resolves the configured base tag over the network.
Tests inject a checked OCI manifest fixture so they remain deterministic.
