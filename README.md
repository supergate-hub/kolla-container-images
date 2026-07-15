# kolla-container-images

This public repository builds and publishes native AMD64 and ARM64 Kolla
container images as public packages under
`ghcr.io/supergate-hub/kolla-container-images`, combines each leaf
into an architecture-neutral multi-architecture manifest, validates its
evidence, and produces a generic digest-bound candidate lock for
Kolla-Ansible.

## 무료 운영 결론

이 이미지 파이프라인을 위해 repository를 여러 개로 나눌 필요가 없다. **Public
repository 하나**에 소스, workflow, 검증 증거를 함께 두고 GHCR package도
Public으로 공개하는 구성이 가장 간단하다. repository를 나누어도 무료 혜택이
늘지 않고, 오히려 설정·권한·증거 추적만 복잡해진다.

Workflow는 repository `GITHUB_TOKEN`으로 package를 생성해 GitHub의 repository
visibility 상속 기본값을 사용한다. 그래도 각 package가 처음 생성된 뒤 실제
visibility가 Public인지 확인하고, 필요하면 명시 전환한 다음 익명 pull을 검증해야
한다. 이 확인 전에는 Public 서빙 완료로 보지 않는다.

현재 GitHub 정책에서 Public repository의 **standard GitHub-hosted runner**
사용은 과금 대상 런너 분(minute) 제한 없이 무료이며, Public GHCR package의
storage와 bandwidth도 무료이다. 다만 larger runner는 Public repository에서도
항상 과금되므로 사용하지 않는다. 이는 영구적 보장이 아니라 현재 GitHub
정책에 따른 설계이므로 주기적으로 billing 정책을 다시 확인한다. Actions의
일반적인 job, concurrency, API 제한은 여전히 적용된다.

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

Builds use only GitHub's standard native hosted runners: `ubuntu-24.04` for
AMD64 and `ubuntu-24.04-arm` for ARM64. Larger runners and privately managed
runner fleets are outside this design. To fit the standard runner's 14 GB
disk, the frozen plan builds exactly one Kolla target per job with
`--threads 1` and `--push-threads 1`; every build matrix uses
`max-parallel: 4`.

The exact dependency DAG is:

```text
parent tier 0 -> parent tier 1 -> parent tier 2
              -> leaf stage 0 -> optional leaf stage 1
              -> aggregate native evidence
              -> multi-arch manifests -> publish summary -> generic candidate lock
              -> hand off to openstack-infra-ops
```

Leaf stage 1 is normally empty. The deployment profile uses it for the real
selected-leaf dependency `ovn-sb-db-server -> ovn-sb-db-relay`. Jobs consume
the immutable digest in the preceding unit's JSON evidence directly; there is
no parent-index artifact.

Each fresh job must have at least 8 GiB free Docker storage after cleanup and
must stay at or above 2 GiB while building. The hosted-only design remains a
feasibility gate until the eight-unit Keystone canary succeeds on both native
architectures. Successful plan, unit, native, and terminal JSON evidence is
retained for seven days; failure diagnostics are retained for one day. Jobs do
not upload Docker layers, image tar files, or Docker caches. The canary is the
practical confirmation that each sharded target fits the advertised runner,
not merely a configuration check.

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
`stream × architecture × build unit`: runner identity, parent ancestry,
immutable child digest, disk measurements, and recorded image platform. Leaf
evidence also records `/bin/true` execution, exact multi-architecture manifest
membership, publish-summary coverage, and generic-lock consistency.

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
scripts/run-build-unit.py        One-target native build, push, disk check, and smoke
scripts/aggregate-native-evidence.py  Exact all-unit evidence aggregator
scripts/validate-kolla-build-summary.py  One-target Kolla summary validator
scripts/validate-publish-approval.py  Non-dry-run approval gate
scripts/validate-publish-summary.py   Publish-summary validator
scripts/generate-lock.py         Generic candidate-lock renderer
.github/workflows/validate.yml   Push and pull-request validation
.github/workflows/publish.yml    Dispatch-only manual and CI publish path
.github/workflows/build-unit.yml Reusable standard hosted one-target build job
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
