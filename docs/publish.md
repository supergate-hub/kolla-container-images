# Publish workflow

The publish workflow is manual-only:

```text
.github/workflows/publish.yml
```

Its delivery contract is:

```text
build -> publish per-architecture images -> create multi-arch manifests
      -> publish summary -> generic candidate lock artifact
      -> hand off to openstack-infra-ops
```

## Inputs and safety default

The workflow accepts `release`, `distro`, `distro_version`, `profile`, `image`,
`dry_run`, and `approval`. `dry_run` defaults to `true`.

A dry run validates configuration, renders the publish plan, and uploads that
plan. It does not build, push, or create manifests. Use this path for routine
planning:

```bash
gh workflow run publish.yml \
  --ref main \
  -f release=2025.1 \
  -f distro=rocky \
  -f distro_version=9 \
  -f profile=core \
  -f image=keystone \
  -f dry_run=true
```

## Explicit approval for publication

Every `dry_run: false` request is rejected unless the matching repository
variable is `true` and the `approval` input exactly matches its scope:

- Keystone smoke: `ALLOW_GHCR_PUBLISH=true`
  and `I approve GHCR smoke publish for keystone 2025.1-rocky-9 from supergate-jhbyun/kolla-container-images.`
- Full core: `ALLOW_GHCR_FULL_CORE_PUBLISH=true`
  and `I approve GHCR full-core publish for core 2025.1-rocky-9 (21 images, amd64/arm64) from supergate-jhbyun/kolla-container-images.`
- Full deployment profile: `ALLOW_GHCR_DEPLOYMENT_PUBLISH=true`
  and `I approve GHCR deployment publish for deployment 2025.1-rocky-9 (52 images, amd64/arm64) from supergate-jhbyun/kolla-container-images.`

The approval gate runs before image build jobs. A gate for one scope does not
authorize another scope.

## Publication sequence

For an approved non-dry-run dispatch, the workflow:

1. Renders a single publish plan and the native runner matrices.
2. Builds and pushes shared parent images once per architecture.
3. Builds and pushes leaf image groups with bounded parallelism.
4. Creates multi-arch manifests only after all architecture jobs complete.
5. Writes and validates the publish summary.
6. For a full profile, generates a generic candidate lock.
7. Uploads the complete artifact set.

The workflow serializes writers for the same release/distro/version tag set and
does not cancel an in-progress publication.

## Artifacts

The publish plan contains build and manifest data, but no environment paths.
The publish summary records scope, per-architecture references, manifest
references, and sha256 manifest digests. A full-profile candidate lock contains
only digest-pinned Kolla-Ansible `*_image_full` variables.

Typical artifact paths are:

```text
artifacts/plan/publish-plan.json
artifacts/publish-summary-2025.1-rocky-9.json
artifacts/kolla-ansible-image-lock-2025.1-rocky-9.yml
```

## Handoff boundary

The uploaded summary and candidate lock are the terminal outputs of this
repository. Hand them to `openstack-infra-ops`. That repository owns
dev/stg/prod promotion, environment pointers, site-specific checks, deployment
orchestration, and rollback policy.
