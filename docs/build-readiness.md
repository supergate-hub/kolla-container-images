# Real build readiness

This document lists the prerequisites for running the manual publish workflow
with `dry_run: false`.

## Runner and tooling requirements

- Native Linux runners for amd64 and arm64.
- Enough disk for Kolla base layers, service image layers, and build cache.
- Python 3 and network access to install the pinned `kolla` package.
- Docker Engine with BuildKit and the Docker Buildx plugin.
- GHCR authentication through `GITHUB_TOKEN`.
- Job-level `packages: write` permission only on jobs that push or finalize
  images.

The workflow uses `ubuntu-24.04` for amd64 and `ubuntu-24.04-arm` for arm64.
It does not use emulation. Shared parents are built once per architecture,
then service groups run in a bounded matrix with at most eight concurrent
jobs.

## Command plan shape

Render a plan locally before publishing:

```bash
python3 scripts/plan-publish.py \
  --profile core \
  --image keystone \
  --release 2025.1 \
  --distro rocky \
  --distro-version 9 \
  --dry-run
```

The planner emits executable command arrays. The workflow executes those
arrays instead of rebuilding shell commands independently.

An amd64 parent command for the keystone smoke target has this shape:

```bash
kolla-build \
  --engine docker \
  --base rocky \
  --base-tag 9 \
  --base-arch x86_64 \
  --platform linux/amd64 \
  --openstack-release 2025.1 \
  --registry ghcr.io \
  --namespace supergate-jhbyun/kolla-container-images \
  --tag 2025.1-rocky-9-amd64 \
  --threads 4 \
  --push-threads 1 \
  --push \
  '^base$' \
  '^openstack-base$' \
  '^keystone-base$'
```

The leaf job pulls the exact parent tags and runs `kolla-build` with
`--skip-existing`. Arm64 commands use `--base-arch aarch64` and
`--platform linux/arm64` on the native arm64 runner.

## Manifest and summary readiness

Do not create an architecture-neutral tag until every selected architecture
job has pushed its image. The final job then creates and inspects each
multi-arch manifest:

```bash
docker buildx imagetools create \
  --tag ghcr.io/supergate-jhbyun/kolla-container-images/keystone:2025.1-rocky-9 \
  --metadata-file artifacts/manifests/keystone-2025.1-rocky-9.json \
  ghcr.io/supergate-jhbyun/kolla-container-images/keystone:2025.1-rocky-9-amd64 \
  ghcr.io/supergate-jhbyun/kolla-container-images/keystone:2025.1-rocky-9-arm64

docker buildx imagetools inspect \
  ghcr.io/supergate-jhbyun/kolla-container-images/keystone:2025.1-rocky-9
```

The final artifact set must include:

- the dry-run publish plan;
- parent and leaf build logs;
- per-architecture image summaries;
- manifest creation and inspection logs;
- a publish summary with a sha256 manifest digest for every selected image;
- for a full-profile publish, a digest-pinned generic candidate lock.

Generate the candidate lock from a complete summary with:

```bash
python3 scripts/generate-lock.py \
  --publish-summary artifacts/publish-summary-2025.1-rocky-9.json \
  --profile deployment \
  --release 2025.1 \
  --distro rocky \
  --distro-version 9 \
  --output artifacts/kolla-ansible-image-lock-2025.1-rocky-9.yml
```

The output contains only digest-pinned Kolla-Ansible `*_image_full` variables.
It is handed to `openstack-infra-ops`, which owns promotion, environment
pointers, deployment validation, and deployment.

## GHCR preflight

- Confirm the intended organization namespace and package visibility policy.
- Confirm the workflow has `packages: write` only where required.
- Confirm the first publish links packages to this repository.
- Confirm anonymous pull behavior after visibility is configured.
- Never bake private CA material, kubeconfigs, registry credentials, OpenStack
  credentials, or site-local configuration into an image.

The first real publish should use `keystone` from the `core` profile because it
has a narrow dependency surface. Stop before publishing unless the exact
non-dry-run approval gate passes.
