# kolla-container-images

This repository builds and publishes native AMD64 and ARM64 Kolla container
images to `ghcr.io/supergate-hub/kolla-container-images`, combines each leaf
into an architecture-neutral multi-architecture manifest, validates its
evidence, and produces a generic digest-bound candidate lock for
Kolla-Ansible.

## Responsibility boundary

```text
build -> publish per-architecture images -> create multi-arch manifests
      -> publish summary -> generic candidate lock artifact
      -> hand off to openstack-infra-ops
```

This repository ends at that generic candidate lock artifact.
`openstack-infra-ops` owns environment-specific locks, Dev/Stg/Prod tags and
pointers, candidate selection, site-specific validation, promotion,
deployment orchestration, and rollback. It also owns external Ceph and site
configuration. No environment state or deployment action belongs here.

Management Kubernetes and workload Kubernetes are separate infrastructure.
They are outside the OpenStack cluster count and outside this repository's
responsibility.

## Supported streams

`config/build-matrix.json` is the source of truth. It selects release, base OS,
and pinned Kolla toolchain as one reviewed stream rather than as free-form
workflow fields.

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
scope variable, exact approval phrase, and protected-environment gate. The six
compatibility streams do not create a standing cluster. When a future stream
becomes primary, external operations first validate its candidate on shared
Stg and only then promote it to Prod.

The `core` profile has 21 leaves in every stream. The stream-resolved
`deployment` closure has these exact leaf counts:

| Release | Rocky | Ubuntu |
| --- | ---: | ---: |
| 2025.1 | 63 | 64 |
| 2025.2 | 63 | 64 |
| 2026.1 | 65 | 66 |

## Operating topology and architecture policy

- Dev consists of 2-3 isolated per-user labs on `bb00` as needed.
- Shared Stg is one HA cluster on `bb01` and `bb02`.
- Prod is one cluster in the Indeokwon IDC.

Current Dev and Stg OpenStack nodes are AMD64. The pipeline policy requires
every stream to be built and image-smoked on native ARM64 CI before its ARM64
artifacts are accepted. A future ARM64 physical node joins the same
OpenStack cluster when it uses the same release and base-OS stream; it does not
create another logical environment. QEMU-only output is not native ARM64
readiness evidence.

The OpenStack node OS and Kolla container base must match. A Rocky 9 lab VM on
an Ubuntu physical host satisfies this rule because the OpenStack node is the
Rocky 9 VM; the hypervisor host OS does not select the container stream.

## Service, storage, and guest-image policy

The deployment profile covers the core OpenStack services plus Cinder, Manila,
Octavia, Valkey, Prometheus, Grafana, Fluentd, OpenSearch, and OpenSearch
Dashboards. Backends are selected by the external deployment system:

| Environment or validation tier | Cinder | Manila |
| --- | --- | --- |
| Dev | Per-lab LVM with LIO and `iscsid` | Manila Generic with DHSS and an NFS share-server VM |
| Stg and Prod | External Ceph RBD | External CephFS NFS through cephadm-managed NFS-Ganesha |
| Compatibility smoke | Disposable LVM with LIO on Rocky or TGT on Ubuntu | Disposable Generic/NFS backend |

Ceph provisioning, pools, identities, `ceph.conf`, keys, and NFS-Ganesha HA
remain external. Standing Dev/Stg/Prod and compatibility smoke all enable and
validate Prometheus, Grafana, Fluentd, OpenSearch, and OpenSearch Dashboards.

Octavia Amphora guest images and Manila Generic share-server guest images live
in Glance. They require architecture-compatible variants and explicit
scheduling for mixed compute, and they are not Kolla container artifacts.
Their build pipelines, Glance storage, and scheduling policy remain external
and are owned by `openstack-infra-ops`.

## Evidence ownership

This repository owns native build and image-smoke evidence keyed by
`stream × architecture × leaf`: runner identity, immutable child digest,
recorded image platform, `/bin/true` execution, exact multi-architecture
manifest membership, publish-summary coverage, and generic-lock consistency.

Matching-OS Kolla-Ansible deployment-smoke evidence is keyed by
`stream × architecture` and remains external. `openstack-infra-ops` or a
dedicated deployment harness owns service-level checks for Keystone, Nova,
Cinder, Manila, Octavia, and observability. Compatibility evidence does not
imply a standing compatibility cluster.

## Image and lock outputs

For candidate ID `123456789-1`, one selected leaf is published under these
native child tags:

```text
ghcr.io/supergate-hub/kolla-container-images/nova-compute:2025.1-rocky-9-candidate-123456789-1-amd64
ghcr.io/supergate-hub/kolla-container-images/nova-compute:2025.1-rocky-9-candidate-123456789-1-arm64
```

After both child digests pass validation, the workflow creates the
candidate multi-architecture reference used by the lock:

```text
ghcr.io/supergate-hub/kolla-container-images/nova-compute:2025.1-rocky-9-candidate-123456789-1
```

Only after the complete candidate artifact is uploaded may the workflow update
this convenience stream alias:

```text
ghcr.io/supergate-hub/kolla-container-images/nova-compute:2025.1-rocky-9
```

The stream alias is convenience-only and is never a candidate-lock input.

The candidate lock supplies tag-only Kolla-Ansible variables and records the
digest binding separately:

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

Kolla-Ansible consumes the root-level tag-only variables. Before deployment,
openstack-infra-ops must verify that each deploy_ref resolves to the recorded
manifest_digest and that immutable_ref returns the same manifest bytes. The
lock selects neither an architecture nor an environment.

## Repository layout

```text
config/build-matrix.json         Seven streams, native architectures, registry, and pins
config/profiles/                 Stream-aware image catalogs and Kolla-Ansible mappings
scripts/validate-config.py       Configuration validator
scripts/plan-publish.py          Read-only frozen-plan renderer
scripts/validate-publish-approval.py  Non-dry-run approval gate
scripts/validate-publish-summary.py   Publish-summary validator
scripts/generate-lock.py         Generic candidate-lock renderer
.github/workflows/validate.yml   Push and pull-request validation
.github/workflows/publish.yml    Dispatch-only manual and CI publish path
docs/build-readiness.md          Native runner and evidence readiness
docs/publish.md                  Publish, consumption, manual setup, and handoff contract
```

## Local validation

```bash
python3 -m json.tool config/build-matrix.json >/dev/null
python3 -m json.tool config/profiles/core.json >/dev/null
python3 -m json.tool config/profiles/deployment.json >/dev/null
python3 scripts/validate-config.py
python3 scripts/plan-publish.py --stream 2025.1-rocky-9 --profile deployment --candidate-id local-dry-run --dry-run
python3 -m unittest discover -s tests -v
```

The planner requires `--dry-run` and performs no registry mutation. Manual
operators and CI each start a separate `workflow_dispatch` run; candidate
identity is derived inside that run, never supplied as an input. Real
publication remains disabled unless every scope-specific approval control
passes. See [docs/publish.md](docs/publish.md) for the exact inputs, artifacts,
manual prerequisites, Kolla-Ansible consumption example, and handoff.
