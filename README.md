# kolla-container-images

This repository builds and publishes multi-architecture Kolla container images
to GHCR. It is the image-provider half of the OpenStack delivery design.

## Responsibility boundary

```text
build -> publish per-architecture images -> create multi-arch manifests
      -> publish summary -> generic candidate lock artifact
      -> hand off to openstack-infra-ops
```

This repository ends at the generic candidate lock artifact. The
`openstack-infra-ops` repository owns dev/stg/prod promotion, environment
pointers, site-specific validation, and deployment. Do not add environment
lock paths or deployment orchestration here.

## Image and lock outputs

Architecture-specific images use tags such as:

```text
ghcr.io/supergate-jhbyun/kolla-container-images/keystone:2025.1-rocky-9-amd64
ghcr.io/supergate-jhbyun/kolla-container-images/keystone:2025.1-rocky-9-arm64
```

After both architecture images exist, the publish workflow creates the
architecture-neutral multi-arch manifest:

```text
ghcr.io/supergate-jhbyun/kolla-container-images/keystone:2025.1-rocky-9
```

A complete publish summary records each manifest digest. From that summary,
`scripts/generate-lock.py` renders a generic Kolla-Ansible extra-vars file with
digest-pinned `*_image_full` values, for example:

```yaml
keystone_image_full: "ghcr.io/supergate-jhbyun/kolla-container-images/keystone:2025.1-rocky-9@sha256:<manifest-digest>"
```

The candidate lock does not select an environment or encode a destination
repository path.

## Profiles

- `core` is the 21-image smoke and core OpenStack profile.
- `deployment` is the 52-image Kolla-Ansible deployment closure.

Both profiles define leaf images, Kolla-Ansible variable mappings, shared
parent dependencies, and bounded service build groups for amd64 and arm64.

## Repository layout

```text
config/build-matrix.json         Supported release, distro, architecture, and registry
config/profiles/                 Image profiles and Kolla-Ansible variable mappings
scripts/validate-config.py       Configuration validator
scripts/plan-publish.py          Dry-run build and manifest planner
scripts/validate-publish-approval.py  Non-dry-run approval gate
scripts/validate-publish-summary.py   Publish summary validator
scripts/generate-lock.py         Publish summary to generic candidate lock renderer
.github/workflows/validate.yml   Push and pull-request validation
.github/workflows/publish.yml    Manual-only publish workflow
docs/build-readiness.md          Runner and command readiness notes
docs/publish.md                  Publish workflow and handoff contract
```

## Local validation

```bash
python3 -m json.tool config/build-matrix.json >/dev/null
python3 -m json.tool config/profiles/core.json >/dev/null
python3 -m json.tool config/profiles/deployment.json >/dev/null
python3 scripts/validate-config.py
python3 scripts/plan-publish.py --profile deployment --release 2025.1 --distro rocky --distro-version 9 --dry-run
python3 -m unittest discover -s tests -v
```

The planner is read-only and requires `--dry-run`. It emits JSON containing
build commands, per-architecture references, manifest commands, publish
summary location, and candidate lock location. It does not publish images.

## Publishing

Publishing is available only through manual workflow dispatch. `dry_run`
defaults to `true`; a non-dry-run request must pass the explicit approval gate
for its exact profile and image scope before any build job can run.

See [docs/publish.md](docs/publish.md) for inputs, approval requirements,
artifacts, and the `openstack-infra-ops` handoff.
