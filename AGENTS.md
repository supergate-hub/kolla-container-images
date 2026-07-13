# Repository boundary

This repository owns Kolla image configuration, native per-architecture
builds, GHCR publication, multi-arch manifests, publish summaries, and generic
digest-pinned candidate lock artifacts.

The required flow is:

```text
build -> publish per-architecture images -> create multi-arch manifests
      -> publish summary -> generic candidate lock artifact
      -> hand off to openstack-infra-ops
```

Stop at the handoff. Dev/stg/prod promotion, environment pointers,
site-specific validation, deployment orchestration, and rollback policy belong
to `openstack-infra-ops`. Do not add environment-specific lock files, promotion
logic, or deployment actions to this repository.
