# Kolla Multi-Stream GHCR Image Supply Design

Date: 2026-07-13
Status: approved by the user on 2026-07-13

> **Supersession note (2026-07-14):** The current execution contract is
> [Kolla Publish Hardening Design](2026-07-14-publish-hardening-design.md).
> Candidate-qualified refs now feed locks, and the stable stream tag is only a
> post-artifact convenience alias. The earlier reusable-trigger contract is
> historical; current CI creates a separate `workflow_dispatch` run so the
> run-derived candidate identity and artifact namespace remain exact.

## 1. Goal

Provide reproducible Kolla container images from the organization-owned registry
`ghcr.io/supergate-hub/kolla-container-images` for Kolla-Ansible deployments.
The image supply must cover:

- OpenStack 2025.1, 2025.2, and 2026.1;
- Rocky Linux 9, Rocky Linux 10, and Ubuntu 24.04 only in combinations that
  the corresponding Kolla release supports;
- native AMD64 and ARM64 builds combined into architecture-neutral
  multi-architecture manifests;
- the core OpenStack deployment plus Cinder, Manila, Octavia, Prometheus,
  Grafana, Fluentd, and OpenSearch;
- digest-bound generic candidate locks suitable for handoff to
  `openstack-infra-ops`.

The immediate safety target is a complete dry-run path. No implementation or
validation step in this workspace authorizes an actual GHCR publish, workflow
dispatch, or repository-variable change.

## 2. Responsibility Boundary

This repository owns:

```text
Kolla configuration
  -> native per-architecture build
  -> per-architecture publish
  -> multi-architecture manifest
  -> digest and publish-summary validation
  -> generic candidate lock
  -> handoff
```

`openstack-infra-ops` owns:

- Dev, Stg, and Prod inventory and Kolla-Ansible configuration;
- environment-specific lock materialization and candidate selection;
- deployment, smoke, promotion, rollback, and environment pointers;
- Ceph configuration, secrets, and site-local networking;
- Glance guest artifacts used by Octavia Amphora and Manila Generic.

This repository must not add Dev, Stg, or Prod tags, lock paths, pointers,
promotion state, deployment orchestration, or rollback logic. Management K8s
and workload K8s are separate infrastructure and are not part of this design.

## 3. Deployment Topology

There are three logical OpenStack environments, not one environment per
release, operating system, or architecture.

| Environment | Placement | Persistent OpenStack clusters | Purpose |
| --- | --- | ---: | --- |
| Dev | Per-user labs on `bb00` | 2-3 as needed | Isolated development and destructive testing |
| Stg | Shared deployment on `bb01` and `bb02` | 1 | HA and promotion validation |
| Prod | Indeokwon IDC | 1 | Production service |

The standing operating stream is `2025.1-rocky-9`. The physical operating
system of a virtualization host does not determine the OpenStack stream. For
example, Rocky 9 OpenStack lab VMs on an Ubuntu `bb00` physical host satisfy
the host/container-base matching policy because the OpenStack nodes are the
Rocky 9 VMs.

Current Dev and Stg nodes are AMD64. ARM64 images are nevertheless built and
validated on native ARM64 infrastructure. Future ARM64 physical nodes may join
the same OpenStack cluster when they use the same release and base-OS stream.
They do not require a separate cluster or architecture-specific Kolla-Ansible
image variables.

## 4. Supported Platform Streams

`config/build-matrix.json` is the source of truth for seven valid streams.
Release, distribution, and distribution version are selected as one stream;
the workflow must not permit unsupported free-form combinations.

| Stream ID and deploy tag | Kolla / Kolla-Ansible pins | Base used by `kolla-build` | Role |
| --- | --- | --- | --- |
| `2025.1-rocky-9` | `20.4.0` / `20.4.0` | Rocky `9` | Standing Dev/Stg/Prod stream |
| `2025.1-rocky-10` | `20.4.0` / `20.4.0` | Rocky `10` | Build/smoke compatibility |
| `2025.1-ubuntu-noble` | `20.4.0` / `20.4.0` | Ubuntu `24.04` | Build/smoke compatibility |
| `2025.2-rocky-10` | `21.1.0` / `21.1.0` | Rocky `10` | Build/smoke compatibility |
| `2025.2-ubuntu-noble` | `21.1.0` / `21.1.0` | Ubuntu `24.04` | Build/smoke compatibility |
| `2026.1-rocky-10` | `22.0.0` / `22.0.0` | Rocky `10` | Build/smoke compatibility |
| `2026.1-ubuntu-noble` | `22.0.0` / `22.0.0` | Ubuntu `24.04` | Build/smoke compatibility |

All seven streams are publish-capable. Publication is still disabled by
default and requires the non-dry-run approval controls in section 8. Rocky 9
must not be added to 2025.2 or 2026.1 without treating it as an unsupported
experimental stream.

The Ubuntu build base tag is `24.04`, while the Kolla-Ansible-compatible image
tag token is `noble`. These values must be separate matrix fields rather than
derived from one another.

## 5. Registry and Reference Contract

The matrix registry identity is:

```text
registry:   ghcr.io
owner:      supergate-hub
repository: kolla-container-images
namespace:  supergate-hub/kolla-container-images
```

Each resolved leaf first has two native references:

```text
ghcr.io/supergate-hub/kolla-container-images/nova-compute:2025.1-rocky-9-candidate-123456789-1-amd64
ghcr.io/supergate-hub/kolla-container-images/nova-compute:2025.1-rocky-9-candidate-123456789-1-arm64
```

Only after both references exist and pass inspection may the workflow create:

```text
ghcr.io/supergate-hub/kolla-container-images/nova-compute:2025.1-rocky-9-candidate-123456789-1
```

The architecture-neutral tag points to either a standard OCI image index
(`application/vnd.oci.image.index.v1+json`) or Docker manifest list
(`application/vnd.docker.distribution.manifest.list.v2+json`) containing
exactly `linux/amd64` and `linux/arm64`. Kolla receives `x86_64` and `aarch64`
as its corresponding base-architecture values. Candidate locks provide a
tag-only Kolla-Ansible variable plus separate multi-architecture manifest
digest evidence:

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

The newer candidate-lock design supersedes the direct-digest consumption
shape. Before deployment, `openstack-infra-ops` verifies that `deploy_ref`
resolves to `manifest_digest` and that `immutable_ref` returns the same bytes;
Kolla-Ansible then consumes the root-level tag-only variable.
The stable `2025.1-rocky-9` stream tag is updated only after the complete
candidate artifact and remains a convenience alias, never a lock input.

Kolla-Ansible consumers set `openstack_tag_suffix: ""`. Docker or Podman on
each node selects the matching child manifest. Neither `globals.yml`, the
inventory, nor the lock selects AMD64 or ARM64.

## 6. Deployment Profile Model

### 6.1 One catalog, stream-aware resolution

Use one stream-aware catalog per profile rather than a universal unconditional
superset or duplicated release profiles. Both `core` and `deployment` use
profile schema version 3 and list the seven reviewed streams. Schema version 3
adds an optional `applies_to` selector to image entries and to
release-specific Kolla-Ansible-variable mappings.

An absent selector means all reviewed streams. A selector may contain exact
stream IDs, matrix releases, or distributions, which are combined with AND
semantics. It may not select an architecture; every resolved leaf must be
available for both architectures.

Examples:

```json
{
  "name": "tgtd",
  "kolla_ansible_variables": ["tgtd_image_full"],
  "applies_to": {"distros": ["ubuntu"]}
}
```

```json
{
  "name": "prometheus-valkey-exporter",
  "kolla_ansible_variables": [
    "prometheus_valkey_exporter_image_full"
  ],
  "applies_to": {"releases": ["2026.1"]}
}
```

The `neutron-server` leaf gains three variable aliases in 2025.2 and 2026.1:
`neutron_rpc_server_image_full`, `neutron_periodic_worker_image_full`, and
`neutron_ovn_maintenance_worker_image_full`. The schema resolves these
variable mappings by release without creating additional container leaves.
An unconditional variable remains a string shorthand. A conditional variable
is an object containing `name` and `applies_to`:

```json
{
  "name": "neutron-server",
  "kolla_ansible_variables": [
    "neutron_server_image_full",
    {
      "name": "neutron_rpc_server_image_full",
      "applies_to": {"releases": ["2025.2", "2026.1"]}
    }
  ]
}
```

Each profile records the seven reviewed stream IDs. Matrix validation fails
when a stream is added without explicit review of both profiles. This prevents
a future release from silently inheriting stale core or deployment mappings.

A single resolver module is used by the planner, publish-summary validator,
and lock generator. It:

1. loads the raw catalog;
2. filters images and variable mappings for the selected stream;
3. filters each build group's image names against the resolved image set;
4. removes empty groups while retaining catalog order;
5. returns the same resolved profile to every downstream stage.

### 6.2 Resolved deployment closure

The common capability choice is OVN networking, Cinder, Manila, Octavia
Amphora/OVN, Valkey, central logging, Grafana, and Prometheus.

| Capability | Deployable leaf images |
| --- | --- |
| Cinder | `cinder-api`, `cinder-scheduler`, `cinder-volume`, `cinder-backup` |
| Manila | `manila-api`, `manila-scheduler`, `manila-share`, `manila-data` |
| Octavia | `octavia-api`, `octavia-driver-agent`, `octavia-health-manager`, `octavia-housekeeping`, `octavia-worker` |
| Coordination/jobboard | `valkey-server`, `valkey-sentinel` |
| LVM runtime | `iscsid`; additionally `tgtd` on Ubuntu only |
| Central logging | `fluentd`, `opensearch`, `opensearch-dashboards` |
| Metrics/UI | `grafana` and the Kolla-Ansible Prometheus server/exporter set |
| 2026.1 additions | `prometheus-openstack-network-exporter`, `prometheus-valkey-exporter` |

Combined with the current core deployment images, the exact unique leaf
counts are:

| Release | Rocky | Ubuntu |
| --- | ---: | ---: |
| 2025.1 | 63 | 64 |
| 2025.2 | 63 | 64 |
| 2026.1 | 65 | 66 |

The existing 21-image `core` profile remains a narrow smoke profile across all
seven streams and uses the same resolver. Release-specific aliases, including
the 2025.2+ Neutron aliases, are resolved in every profile containing the
affected leaf. The stream-aware counts above apply only to `deployment`: its
existing 52 leaves gain 11 common leaves for Cinder, Manila, `iscsid`, and
Valkey; Ubuntu adds `tgtd`; 2026.1 adds the two Prometheus exporters.

The following are intentionally excluded:

- `multipathd`, because LVM and Ceph RBD do not enable multipath by default;
- `etcd`, because Valkey satisfies Cinder coordination and Octavia requires
  Valkey for the selected Amphora jobboard path;
- Redis, because it is only an alternative in 2025.1 and is rejected or
  removed in later releases;
- Ceph daemon images, because Ceph is external;
- inactive optional leaves such as Bifrost, Ironic, Swift, Designate,
  OVS-DPDK, SPICE, federation-only HTTPD, and Let's Encrypt-only HAProxy SSH.

Rocky uses Kolla-Ansible's default `lioadm` Cinder target helper, so `tgtd` is
not deployed and must not be built there. Ubuntu uses the default `tgtadm`
path and therefore includes `tgtd`. Overriding these target-helper defaults is
outside this design and requires a profile policy change.

### 6.3 Profile invariants

Configuration validation checks both the raw catalog and every resolved
stream:

- each leaf and each resolved Kolla-Ansible variable is unique;
- every raw and resolved leaf belongs to exactly one build group;
- selectors only reference matrix values and never architectures;
- every parent chain is valid;
- Cinder, Manila, Octavia, and observability minimum sets exist in all streams;
- `tgtd` exists only in Ubuntu streams;
- the two new exporters exist only in 2026.1 streams;
- 2025.2+ Neutron variable aliases resolve to `neutron-server`;
- every resolved leaf produces both native references and one
  multi-architecture manifest reference;
- publish summary and candidate lock coverage exactly match the resolved
  profile.

## 7. Build and Workflow Design

### 7.1 Triggers and inputs

Historical trigger contract (superseded): one implementation path served both
`workflow_dispatch` and `workflow_call`. The current contract starts a separate
`workflow_dispatch` run for manual and CI publication.
GitHub cannot dynamically populate a `workflow_dispatch` choice list from
JSON, so `stream` is a string input that is strictly validated against the
matrix before planning. Invalid input fails with the seven accepted values.
Inputs are:

- `stream`: one of the seven matrix IDs;
- `profile`: `core` or `deployment`;
- `image`: a resolved leaf or `all`;
- `dry_run`: defaults to `true`;
- `approval`: ignored for dry runs and validated exactly for publication.

CI callers may run validation and dry-run planning automatically. They cannot
bypass the non-dry-run approval gate. Adding scheduled release discovery or
automatically following a moving upstream branch is outside this design.

### 7.2 Native build unit

The build unit is one selected stream, profile, and native architecture. For a
full profile, run one dependency-aware `kolla-build` invocation per
architecture rather than exploding the workflow into one job per service
group. Kolla builds required parents and parallelizes independent images with
bounded `--threads`.

Profile build groups remain logical catalog, validation, reporting, and
single-image dependency units. They do not define full-profile workflow job
fan-out.

```text
selected stream
  +-- native AMD64 job: one resolved-profile Kolla build
  +-- native ARM64 job: one resolved-profile Kolla build
  `-- finalize job after both jobs succeed
```

Use a directly created Python virtual environment with the matrix-pinned Kolla
package, exact Docker SDK pin `docker==7.1.0`, and a pip cache. Import the SDK,
verify its pin, and exercise a harmless Kolla CLI path before registry login.
Do not introduce a custom `kolla-build-venv` image until measured setup cost
justifies owning another artifact. Do not follow a moving `stable/*` branch
during a publish.

Real builds require native Linux runners with adequate ephemeral storage.
Target at least 150 GB and prefer about 300 GB for a complete profile and
cache. Cross-stream concurrency is bounded to two to four build jobs. Writers
for the same stream are serialized and an in-progress publish is not
cancelled.

QEMU may assist local debugging, but its output is never ARM64 publication or
deployment approval evidence.

### 7.3 Dry run

A dry run performs no registry mutation. It validates configuration and emits
the complete intended plan, including:

- pinned toolchain and Kolla parameters;
- the resolved profile and build groups;
- every native build command and per-architecture reference;
- every multi-architecture manifest reference and command;
- publish-summary and candidate-lock artifact paths;
- the exact non-dry-run approval phrase for the selected scope.

## 8. Publication Safety

Non-dry-run scope is deliberately limited to these existing approval classes:

| Scope | Required repository variable |
| --- | --- |
| `core/keystone` | `ALLOW_GHCR_PUBLISH=true` |
| `core/all` | `ALLOW_GHCR_FULL_CORE_PUBLISH=true` |
| `deployment/all` | `ALLOW_GHCR_DEPLOYMENT_PUBLISH=true` |

All other partial-image scopes are dry-run only. `dry_run: false` is permitted
only when all of the following hold:

1. the existing repository variable for the requested scope is `true`;
2. the caller supplies the exact phrase
   `PUBLISH ghcr.io/supergate-hub/kolla-container-images <stream> <profile>/<image> (<resolved-count> image[s], amd64/arm64)`, using `image` only when the count is one;
3. the protected GitHub environment for GHCR publication grants approval;
4. configuration and plan validation succeed before any build starts.

For example:

```text
PUBLISH ghcr.io/supergate-hub/kolla-container-images 2025.1-rocky-9 deployment/all (63 images, amd64/arm64)
```

An approval for one stream, profile, or image cannot authorize another. Full
candidate locks are generated only for `profile=deployment` and `image=all`;
a core, Keystone, or partial deployment publish cannot produce a complete
lock. Including the resolved image count and both architectures preserves the
existing scope-drift protection when stream closures differ.

Build jobs may use `packages: write` only where pushing is required. Dry-run
and validation jobs remain read-only. The workflow uses `GITHUB_TOKEN` for
repository-linked package publication and never embeds registry credentials,
OpenStack credentials, Ceph keys, private CAs, or site configuration in an
image.

## 9. Publish Data Flow and Failure Semantics

For a real, approved publication:

1. Resolve and freeze one plan.
2. Build and push required AMD64 parent images, then every selected leaf, and
   record all architecture-specific digests.
3. Build and push required ARM64 parent images, then every selected leaf, and
   record all architecture-specific digests.
4. Reject finalization if either architecture is incomplete.
5. Create and inspect each multi-architecture manifest from the two recorded
   child digests.
6. Verify that each manifest uses one of the two allowed standard media types
   and contains exactly the expected platforms.
7. Write and validate a publish summary covering the resolved profile.
8. For a full `deployment/all` run, render a generic lock from
   multi-architecture manifest digests.
9. Upload plan, logs, architecture summaries, manifest evidence, publish
   summary, and candidate lock.

Parent and base images are necessary build artifacts and are authorized by
the selected leaf/profile scope. The plan and per-architecture build summaries
list their refs and digests. They retain architecture-specific tags and do not
receive architecture-neutral manifests. The deploy publish summary, manifest
set, and candidate lock contain resolved deployable leaves only because
Kolla-Ansible never consumes the parent images directly.

Missing or duplicate images, missing variable mappings, unexpected platforms,
non-SHA256 digests, scope mismatches, and partial full-profile summaries are
hard failures. No lock is produced from incomplete evidence.

The terminal artifacts use stream-specific, environment-neutral paths such
as:

```text
artifacts/plan/publish-plan.json
artifacts/publish-summary-2025.1-rocky-9.json
artifacts/kolla-ansible-image-lock-2025.1-rocky-9.yml
```

## 10. Mixed Storage and Observability

| Environment or validation tier | Cinder | Manila |
| --- | --- | --- |
| Dev `2025.1-rocky-9` | Per-lab LVM with LIO and `iscsid` | Generic driver with DHSS and NFS share-server VM |
| Stg and Prod | External Ceph RBD | External CephFS through cephadm-managed NFS-Ganesha |
| Compatibility stream smoke | Disposable LVM; LIO on Rocky and TGT on Ubuntu | Disposable Generic/NFS backend |

Ceph provisioning, pools, CephX identities, `ceph.conf`, keyrings, and
NFS-Ganesha HA belong to the external Ceph and operations domains. CephFS NFS
is selected for Stg and Prod so tenant VMs do not require direct access to the
Ceph public network. Changing to CephFS Native does not change the Kolla leaf
closure but does change the network and credential trust model.

All standing Dev, Stg, and Prod OpenStack deployments enable Prometheus,
Grafana, Fluentd, OpenSearch, and OpenSearch Dashboards. Compatibility smoke
also validates these components rather than treating monitoring as an optional
post-deployment add-on.

Two required artifacts are explicitly outside the Kolla container registry:

- Octavia Amphora guest images stored in Glance;
- Manila Generic share-server guest images stored in Glance.

For future mixed-architecture compute, operations must provide and schedule
architecture-compatible variants of these guest images. A multi-architecture
Kolla container manifest does not solve guest-appliance image selection.

## 11. Deployment Consumption Contract

`globals.yml` carries registry and stream defaults, not a hand-written list of
all service references:

```yaml
docker_registry: "ghcr.io"
docker_namespace: "supergate-hub/kolla-container-images"
docker_registry_insecure: "no"

openstack_release: "2025.1"
kolla_base_distro: "rocky"
kolla_base_distro_version: "9"
openstack_tag_suffix: ""
```

The generic digest lock is supplied separately through an operations-managed
`globals.d` file or an explicit extra-vars file. Environment-specific files
enable LVM/Generic or Ceph RBD/CephFS NFS and provide their non-secret
configuration. Secrets remain in Kolla `passwords.yml`, an encrypted secret
store, or protected host files as appropriate.

The same `2025.1-rocky-9` candidate lock and its per-image manifest digests are
promoted through Dev, Stg, and Prod. Promotion selects already published
evidence; it does not rebuild or retag an environment-specific image in this
repository.

## 12. Smoke and Readiness Evidence

Validation has two ownership layers:

1. This repository verifies native build completion, native child digests,
   per-architecture image smoke, multi-architecture manifest platform
   membership, publish-summary completeness, and lock consistency. Image smoke
   runs on the corresponding native architecture and uses profile-defined
   checks that do not require a site deployment.
2. `openstack-infra-ops` or a dedicated deployment harness consumes the lock
   on matching-OS native hosts and performs Kolla-Ansible prechecks and service
   smoke. Deployment logic is not copied into this repository.

The minimum native image smoke for every child digest is deterministic: verify
the runner architecture, pull by digest, inspect the recorded image platform,
and start the image with its entrypoint overridden to `/bin/true`. This proves
that the intended architecture can be pulled and executed without emulation;
it does not substitute for the service-level deployment smoke in layer 2.

Image evidence is keyed by `stream × architecture × leaf`; deployment smoke
evidence is keyed by `stream × architecture`. The 2025.1 Rocky 9 AMD64 stream
is exercised continuously through Dev and Stg. Every other stream and the
ARM64 side of the standing stream requires a matching-OS, native-architecture
smoke record before being declared compatible, but this does not create a
standing cluster. Smoke checks cover at minimum:

- Keystone authentication and service catalog;
- Nova instance boot and network connectivity;
- Cinder volume create, attach, detach, and delete;
- Manila share create, mount, unmount, and delete;
- Octavia load balancer provisioning and health;
- Prometheus target health, Grafana readiness, and OpenSearch ingestion.

When a future release becomes the primary stream, its candidate is first
validated against the shared Stg external-Ceph configuration and only then
promoted to Prod. Neither native image smoke nor native deployment smoke can
be replaced by QEMU-only results for ARM64 readiness.

## 13. Manual GitHub and GHCR Prerequisites

Before the first real publish, an organization administrator must:

- confirm Actions is permitted to create and write organization packages;
- configure the protected publication environment and required reviewers;
- set the existing scope-specific publication variables only when a publish is
  intentionally authorized;
- confirm `GITHUB_TOKEN` has `packages: write` only in publishing jobs;
- link newly created GHCR packages to this repository and grant Actions access;
- choose package visibility and verify pull behavior;
- provide a read-only `read:packages` service account to Kolla hosts if
  packages remain private;
- register and secure native AMD64 and ARM64 runners with sufficient disk;
- verify retention, vulnerability scanning, and package cleanup policy.

No workflow dispatch, package creation, package visibility change, repository
variable change, or credential provisioning is part of the current workspace
work.

## 14. Verification and Acceptance

Implementation follows test-first namespace and stream changes. Tests must
first fail while the old personal namespace or incomplete stream closure is
still present, then pass after implementation.

Required acceptance checks are:

```text
all JSON files parse
python3 scripts/validate-config.py
core/deployment and Keystone dry-run plans
python3 -m unittest discover -s tests -v
actionlint .github/workflows/*.yml
repository-wide search for the old personal namespace
git diff --check
```

Additional tests assert:

- all plan, native ref, multi-architecture manifest ref, summary, approval,
  and lock values use `supergate-hub/kolla-container-images`;
- all seven streams resolve and use their exact Kolla/base/tag values;
- resolved deployment counts are 63/64/65/66 as specified;
- Ubuntu includes `tgtd`, Rocky excludes it, and 2026.1 alone includes the two
  new exporters;
- 2025.2+ Neutron aliases map to the existing `neutron-server` leaf;
- AMD64 and ARM64 references exist for every resolved leaf;
- non-dry-run publication fails without exact approval;
- the exact variable and count-bearing approval phrase accepts each of the
  seven streams in all three allowed non-dry-run scopes without performing a
  push, including all Ubuntu 24.04 streams;
- every other partial-image scope is rejected for non-dry-run publication;
- a full deployment summary produces a digest-bound generic lock;
- partial publication cannot produce a candidate lock;
- environment names, promotion state, pointers, and deployment actions remain
  absent from repository-owned artifacts.

## 15. Delivery Sequence

1. Implement and validate the organization namespace and stream-aware dry-run
   design without publishing.
2. Complete GitHub, GHCR, and native-runner manual prerequisites.
3. Use the narrow Keystone scope for the first separately approved real
   publish.
4. Publish and validate the full `2025.1-rocky-9` candidate.
5. Hand the generic lock to operations for Dev, Stg, and Prod validation.
6. Build and smoke the six compatibility streams without creating standing
   clusters.
7. Promote a later primary release through existing Stg and Prod using the
   operations repository.

This sequence does not grant approval for steps 2-7 during the current work.

## 16. Known Risks and Required Follow-up

- Upstream Kolla-Ansible does not provide a functionally tested (`T`) ARM64
  matrix for these service families. The selected images are generally
  untested (`U`), so native organization-owned smoke evidence is the support
  basis.
- Valkey exists in the 2025.1 and 2025.2 Kolla/Kolla-Ansible source and is not
  marked unbuildable, but those releases do not list it explicitly in the
  published ARM matrix. Native build and smoke are mandatory before declaring
  those streams ready.
- Full Kolla builds are disk- and network-intensive. Runner capacity must be
  proven with dry-run estimates and the first separately approved Keystone
  publish before attempting a complete profile.
- Every Kolla leaf becomes a separate GHCR package. Organization visibility,
  repository linkage, Actions access, retention, and pull policy need an
  auditable bulk-management procedure outside this code change.
- Octavia Amphora and Manila Generic guest-image pipelines need a separate
  architecture-aware design before ARM64 compute nodes host those appliances.
- Pinned Kolla patch versions do not move automatically. Updating a pin must
  be a reviewed matrix change that repeats build, manifest, smoke, summary,
  and lock validation.
