# Kolla Container Images

[![Validate](https://github.com/supergate-hub/kolla-container-images/actions/workflows/validate.yml/badge.svg)](https://github.com/supergate-hub/kolla-container-images/actions/workflows/validate.yml)
[![Publish](https://github.com/supergate-hub/kolla-container-images/actions/workflows/publish.yml/badge.svg)](https://github.com/supergate-hub/kolla-container-images/actions/workflows/publish.yml)

Reproducible, native AMD64 and ARM64 OpenStack Kolla images published to GHCR.
This repository freezes reviewed Kolla streams, builds every architecture on a
native GitHub-hosted runner, assembles multi-architecture manifests, and emits
a digest-bound candidate lock for Kolla-Ansible.

> [!IMPORTANT]
> A stream tag is a mutable convenience alias. Deployment automation must use
> the candidate reference in the generated lock and verify its recorded digest.
> This repository publishes candidates; it does not promote or deploy them.

## At a glance

| | Contract |
| --- | --- |
| Registry namespace | `ghcr.io/supergate-hub/kolla-container-images` |
| Platforms | `linux/amd64`, `linux/arm64` |
| Build streams | 7 reviewed Kolla, base-OS, and toolchain combinations |
| Image profiles | `core` and `deployment` |
| Publication | Dispatch-only `workflow_dispatch`; `dry_run: true` by default |
| Terminal output | Publish summary and, for `deployment/all`, a generic candidate lock |
| Handoff | `openstack-infra-ops` |

Start with the [publish and consumption contract](docs/publish.md) for operator
instructions, or [build readiness](docs/build-readiness.md) for native runner,
storage, and evidence requirements.

## Responsibility boundary

The repository owns one supply pipeline:

```text
build -> publish per-architecture images -> create multi-arch manifests
      -> publish summary -> generic candidate lock artifact
      -> hand off to openstack-infra-ops
```

It owns Kolla image configuration, native builds and image smoke tests, GHCR
publication, multi-architecture manifest validation, publish summaries, and
generic digest-bound candidate locks.

The handoff is the boundary. `openstack-infra-ops` owns candidate selection,
environment-specific locks and pointers, Dev/Stg/Prod promotion, site-specific
validation, deployment orchestration, and rollback. No environment state or
deployment action belongs in this repository.

## Using published outputs

### Reference model

For candidate ID `123456789-1`, `nova-compute` is published as two native
children and one multi-architecture candidate:

```text
# Native children used to assemble the manifest
ghcr.io/supergate-hub/kolla-container-images/nova-compute:2025.1-rocky-9-candidate-123456789-1-amd64
ghcr.io/supergate-hub/kolla-container-images/nova-compute:2025.1-rocky-9-candidate-123456789-1-arm64

# Candidate reference recorded by the lock
ghcr.io/supergate-hub/kolla-container-images/nova-compute:2025.1-rocky-9-candidate-123456789-1

# Mutable convenience alias; never a lock input
ghcr.io/supergate-hub/kolla-container-images/nova-compute:2025.1-rocky-9
```

Docker or Podman selects the host architecture from the candidate manifest. To
inspect its platforms without pulling all layers:

```bash
docker buildx imagetools inspect \
  ghcr.io/supergate-hub/kolla-container-images/nova-compute:2025.1-rocky-9-candidate-123456789-1
```

### Candidate lock

The generated YAML exposes tag-only variables that Kolla-Ansible can consume
and keeps the supply proof under the reserved `_kolla_candidate_lock` key:

```yaml
_kolla_candidate_lock:
  schema_version: 1
  stream: "2025.1-rocky-9"
  scope:
    profile: "deployment"
    image: "all"
    image_count: 63
  images:
    "nova-compute":
      deploy_ref: "ghcr.io/supergate-hub/kolla-container-images/nova-compute:2025.1-rocky-9-candidate-123456789-1"
      manifest_digest: "sha256:<multi-arch-manifest-digest>"
      immutable_ref: "ghcr.io/supergate-hub/kolla-container-images/nova-compute@sha256:<multi-arch-manifest-digest>"
      kolla_ansible_variables:
        - "nova_compute_image_full"
nova_compute_image_full: "ghcr.io/supergate-hub/kolla-container-images/nova-compute:2025.1-rocky-9-candidate-123456789-1"
```

Before deployment, `openstack-infra-ops` must verify that every `deploy_ref`
resolves to its recorded `manifest_digest` and that the corresponding
`immutable_ref` returns the same manifest bytes. The lock selects neither an
architecture nor an environment.

## Supported streams and profiles

[`config/build-matrix.json`](config/build-matrix.json) is the source of truth.
Each stream pins the OpenStack release, Kolla and Kolla-Ansible versions, base
distribution, and deploy tag as one reviewed unit.

| Stream ID and deploy tag | Kolla / Kolla-Ansible pins | Kolla build base | Role |
| --- | --- | --- | --- |
| `2025.1-rocky-9` | `20.4.0` / `20.4.0` | Rocky `9` | Standing Dev/Stg/Prod baseline |
| `2025.1-rocky-10` | `20.4.0` / `20.4.0` | Rocky `10` | Build, manifest, digest, native-smoke, and lock compatibility |
| `2025.1-ubuntu-noble` | `20.4.0` / `20.4.0` | Ubuntu `24.04`; deploy token `noble` | Build, manifest, digest, native-smoke, and lock compatibility |
| `2025.2-rocky-10` | `21.1.0` / `21.1.0` | Rocky `10` | Build, manifest, digest, native-smoke, and lock compatibility |
| `2025.2-ubuntu-noble` | `21.1.0` / `21.1.0` | Ubuntu `24.04`; deploy token `noble` | Build, manifest, digest, native-smoke, and lock compatibility |
| `2026.1-rocky-10` | `22.0.0` / `22.0.0` | Rocky `10` | Build, manifest, digest, native-smoke, and lock compatibility |
| `2026.1-ubuntu-noble` | `22.0.0` / `22.0.0` | Ubuntu `24.04`; deploy token `noble` | Build, manifest, digest, native-smoke, and lock compatibility |

All seven streams are publish-capable behind the same `dry_run: true` default,
scope-specific approval phrase, and protected-environment gate. Compatibility
streams produce build, manifest, digest, native-smoke, and lock evidence but do
not create a standing compatibility cluster.

Profiles are resolved per stream from [`config/profiles/`](config/profiles/):

- `core` contains 21 leaves in every stream.
- `deployment` is the full stream-aware closure used to generate a candidate
  lock. Its exact leaf counts are:

| Release | Rocky | Ubuntu |
| --- | ---: | ---: |
| 2025.1 | 63 | 64 |
| 2025.2 | 63 | 64 |
| 2026.1 | 65 | 66 |

## Publication and trust model

[`publish.yml`](.github/workflows/publish.yml) is dispatch-only. A run freezes
its stream, profile, optional image, candidate ID, exact build DAG, output
paths, and approval phrase before any writer receives registry access.

The native dependency graph is:

```text
parent tier 0 -> parent tier 1 -> parent tier 2
              -> leaf stage 0 -> optional leaf stage 1
              -> aggregate native evidence
              -> multi-arch manifests -> publish summary -> generic candidate lock
              -> hand off to openstack-infra-ops
```

Leaf stage 1 is normally empty. The deployment profile uses it for the selected
leaf dependency `ovn-sb-db-server -> ovn-sb-db-relay`. Downstream jobs consume
the preceding unit's immutable digest from raw JSON evidence; there is no
parent-index artifact.

Every unit builds one target with `--threads 1` and `--push-threads 1`. The
pipeline uses standard native hosted runners only:

| Architecture | GitHub runner | Recorded platform |
| --- | --- | --- |
| AMD64 | `ubuntu-24.04` | `linux/amd64` |
| ARM64 | `ubuntu-24.04-arm` | `linux/arm64` |

The pipeline policy requires every stream to be built and image-smoked on
native ARM64 CI before its ARM64 artifacts are accepted. QEMU output may help
local debugging, but it is not native ARM64 readiness evidence. Every build
matrix uses `max-parallel: 4`; disk and feasibility gates are documented in
[build readiness](docs/build-readiness.md).

This repository owns native build and image-smoke evidence keyed by
`stream × architecture × build unit`: runner identity, parent ancestry,
immutable child digest, disk measurements, recorded image platform, and leaf
execution smoke. It also proves exact multi-architecture membership,
publish-summary coverage, and generic-lock consistency.

Matching-OS Kolla-Ansible deployment-smoke evidence is keyed by
`stream × architecture` and remains external. `openstack-infra-ops` or a
dedicated deployment harness owns service-level validation.

### Plan safely

Render the smallest useful local plan first. The planner is read-only and
requires `--dry-run`:

```bash
python3 scripts/plan-publish.py \
  --stream 2025.1-rocky-9 \
  --profile core \
  --image keystone \
  --candidate-id local-dry-run \
  --dry-run
```

Repository operators can then create an equivalent GitHub Actions dry run:

```bash
gh workflow run publish.yml \
  --ref main \
  --field stream=2025.1-rocky-9 \
  --field profile=core \
  --field image=keystone \
  --field dry_run=true
```

Real publication additionally requires the frozen plan's exact approval
phrase, the matching repository variable, and the protected `ghcr-publish`
environment. See [Publishing](docs/publish.md) before attempting it. The
current public-runner and public-GHCR cost assumptions are documented there as
operational assumptions, not permanent guarantees.

## External deployment context

The following context explains why profiles and compatibility checks contain
particular services. It is informational only and remains owned by
`openstack-infra-ops`.

<details>
<summary>Deployment topology, service backends, and guest-image policy</summary>

Current operating topology:

- Dev consists of 2-3 isolated per-user labs on `bb00` as needed.
- Shared Stg is one HA cluster on `bb01` and `bb02`.
- Prod is one cluster in the Indeokwon IDC.

Current Dev and Stg OpenStack nodes are AMD64. A future ARM64 physical node
joins the same OpenStack cluster only when it uses the same release and base-OS
stream. The OpenStack node OS and Kolla container base must match. A Rocky 9
lab VM on an Ubuntu physical host satisfies this rule because the VM is the
OpenStack node; the hypervisor OS does not select the container stream.

Management Kubernetes and workload Kubernetes are separate infrastructure and
outside this repository. Compatibility streams do not create a standing cluster.

The deployment profile covers core OpenStack services plus Cinder, Manila,
Octavia, Valkey, Prometheus, Grafana, Fluentd, OpenSearch, and OpenSearch
Dashboards. The external deployment system chooses their backends:

| Environment or validation tier | Cinder | Manila |
| --- | --- | --- |
| Dev | Per-lab LVM with LIO and `iscsid` | Manila Generic with DHSS and an NFS share-server VM |
| Stg and Prod | External Ceph RBD | External CephFS NFS through cephadm-managed NFS-Ganesha |
| Compatibility smoke | Disposable LVM with LIO on Rocky or TGT on Ubuntu | Disposable Generic/NFS backend |

Ceph provisioning, pools, identities, `ceph.conf`, keys, and NFS-Ganesha HA
remain external. Standing environments and compatibility smoke enable and
validate Prometheus, Grafana, Fluentd, OpenSearch, and OpenSearch Dashboards.

Octavia Amphora and Manila Generic share-server guest images live in Glance.
They require architecture-compatible variants and explicit scheduling for
mixed compute, and they are not Kolla container artifacts. Their build,
storage, and scheduling policy remain external.

</details>

## Repository layout

| Path | Purpose |
| --- | --- |
| `config/build-matrix.json` | Stream, architecture, registry, tag, and toolchain pins |
| `config/profiles/` | Stream-aware image catalogs and Kolla-Ansible variable mappings |
| `scripts/validate-config.py` | Static configuration and dependency validation |
| `scripts/plan-publish.py` | Read-only frozen-plan renderer |
| `scripts/run-build-unit.py` | One-target native build, push, disk check, and smoke |
| `scripts/aggregate-native-evidence.py` | Exact all-unit evidence aggregation |
| `scripts/validate-publish-summary.py` | Multi-architecture publish-summary validation |
| `scripts/generate-lock.py` | Generic candidate-lock renderer |
| `.github/workflows/validate.yml` | Pull request and push validation |
| `.github/workflows/publish.yml` | Manual dry-run and approved publication path |
| `.github/workflows/build-unit.yml` | Reusable one-target native build job |
| `docs/publish.md` | Inputs, approvals, artifacts, consumption, and handoff |
| `docs/build-readiness.md` | Native runners, storage gates, evidence, and recovery |

## Local validation

The configuration planner and unit suite use Python's standard tooling; no
registry credentials are needed for these checks:

```bash
python3 -m json.tool config/build-matrix.json >/dev/null
python3 -m json.tool config/profiles/core.json >/dev/null
python3 -m json.tool config/profiles/deployment.json >/dev/null
python3 scripts/validate-config.py
python3 scripts/plan-publish.py \
  --stream 2025.1-rocky-9 \
  --profile deployment \
  --candidate-id local-dry-run \
  --dry-run >/dev/null
python3 -m unittest discover -s tests -v
```

## Upstream and license

This pipeline builds with the upstream [OpenStack Kolla](https://docs.openstack.org/kolla/latest/)
toolchain and produces configuration for
[Kolla-Ansible](https://docs.openstack.org/kolla-ansible/latest/). Upstream's
published images are intended primarily for testing and demonstration;
production operators should curate and validate their own image set.

Licensed under the [Apache License 2.0](LICENSE).
