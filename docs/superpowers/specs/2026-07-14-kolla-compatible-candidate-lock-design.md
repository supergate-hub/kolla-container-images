# Kolla-Ansible-Compatible Candidate Lock Design

> **Supersession note (2026-07-14):** The current execution contract is
> [Kolla Publish Hardening Design](2026-07-14-publish-hardening-design.md).
> This document's dual-contract shape remains valid, but candidate-qualified
> refs now feed the lock. The stable stream tag is only a post-artifact
> convenience alias and is never a lock input.

## Goal

Keep the repository's generic, digest-bound candidate handoff while making the
same YAML artifact safe to load as Kolla-Ansible extra variables for the pinned
20.4.0, 21.1.0, and 22.0.0 releases.

## Problem

The current lock writes each `*_image_full` variable as
`repository:tag@sha256:digest`. All three pinned Kolla-Ansible releases split
an image at its final colon before calling the Docker or Podman SDK. That turns
the digest hex into a tag and removes the `sha256:` digest semantics, so the
generated variables are not a valid direct deployment contract.

GHCR digest references remain the authoritative immutable identity. A normal
architecture-neutral tag remains the only reference shape that the pinned
Kolla-Ansible container workers can consume without an external patch.

## Decision

Generate one dual-contract candidate lock:

1. Root-level `*_image_full` variables contain the normal multi-architecture
   deploy tag, for example
   `ghcr.io/supergate-hub/kolla-container-images/nova-compute:2025.1-rocky-9-candidate-123456789-1`.
2. A reserved `_kolla_candidate_lock` mapping binds every image to its deploy
   reference, manifest digest, and immutable `repository@sha256:digest`
   reference.

Representative shape:

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

The reserved mapping is ordinary Ansible data and is ignored by Kolla-Ansible
roles. The root-level variables remain directly consumable.

## Integrity and Handoff Contract

This repository validates each manifest digest against exact raw registry
bytes before generating the lock. It then records both the Kolla-compatible
tag and immutable digest reference in one artifact.

`openstack-infra-ops` remains responsible for environment-specific use. Before
passing the root-level variables to Kolla-Ansible, it must verify that every
`deploy_ref` resolves to the recorded `manifest_digest` and that the matching
`immutable_ref` returns the same manifest bytes.
Environment locks, promotion, deployment-time verification, rollback, and
site policy stay outside this repository.

The artifact is therefore digest-bound at the supply handoff, while runtime
digest enforcement is explicit downstream work. The repository does not claim
that the pinned Kolla-Ansible versions pull by digest.

## Rejected Alternatives

- Patching or forking Kolla-Ansible would preserve direct digest pulls but
  introduces a three-release deployment dependency outside this repository's
  ownership.
- Publishing digest-derived alias tags still relies on mutable GHCR tags and
  adds registry state without eliminating downstream digest verification.

## Implementation Scope

- Change only candidate-lock rendering, its tests, and affected operator
  documentation.
- Keep publish summaries, manifest validation, artifact paths, approval gates,
  native architecture policy, and `deployment/all` eligibility unchanged.
- Add no external dependency, deployment action, environment pointer,
  promotion state, or GHCR mutation.

## Verification

- Add a regression test that demonstrates the old `tag@digest` value is
  incompatible with the shared pinned Kolla-Ansible parsing contract.
- Require every generated `*_image_full` value to be a tag-only deploy ref
  that parses to the expected repository and stream tag.
- Require metadata for every resolved image to match the validated summary's
  deploy reference and manifest digest and to contain the exact immutable ref.
- Retain all existing summary, complete-scope, duplicate-variable, namespace,
  repository-boundary, workflow, JSON, `actionlint`, and diff checks.
