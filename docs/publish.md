# Publish workflow

`.github/workflows/publish.yml` has `workflow_dispatch` as its sole trigger.
Manual operators and CI automation each create a separate workflow run:

```text
freeze plan -> authorize -> publish native children -> create multi-architecture manifests
            -> publish summary -> generic candidate lock when eligible
            -> hand off to openstack-infra-ops
```

## 무료 public 설계 확정

이 workflow는 **Public repository 하나**에서 운영한다. repository를
AMD64용과 ARM64용으로 나누지 않는다. 분리해도 비용 절감 효과가 없고,
승인·설정·artifact·package 권한만 중복되기 때문이다.

현재 GitHub 정책상 Public repository의 standard GitHub-hosted runner는
과금 대상 런너 분 제한 없이 무료이다. 따라서 AMD64는 `ubuntu-24.04`,
ARM64는 `ubuntu-24.04-arm`만 사용한다. Larger runner는 Public
repository에서도 항상 과금되므로 사용하지 않는다. Public GHCR
package의 storage와 bandwidth도 현재 정책상 무료이다. 이 비용 가정은
GitHub 정책이 바뀌면 다시 검토해야 하며, 일반적인 Actions 서비스 한도는
여전히 적용된다.
검토 기준은 GitHub의 [Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions),
[larger runners](https://docs.github.com/en/actions/concepts/runners/larger-runners),
그리고 [Packages billing](https://docs.github.com/en/enterprise-cloud@latest/billing/concepts/product-billing/github-packages?apiVersion=2022-11-28)
공식 문서이다.

The plan job rejects a repository identity other than
`supergate-hub/kolla-container-images` before checkout. Every workflow run
derives its candidate ID from `github.run_id` and `github.run_attempt`; neither
manual operators nor CI pass candidate identity. Local read-only planning uses
the reserved `local-dry-run` candidate ID.

CI dispatches `publish.yml` on the reviewed protected ref with `--ref main`
(or Actions API `ref: main`). Its repository-scoped GitHub App installation
token, or equivalent short-lived credential, has `Actions: write` and no
package-write permission. The dispatched workflow's parent/leaf build units
and `finalize-publish` separately receive `packages: write` from their
job-scoped `GITHUB_TOKEN` policy.

## Inputs and dry-run default

`workflow_dispatch` exposes exactly these five frozen-scope inputs:

| Input | Contract |
| --- | --- |
| `stream` | Exact ID from `config/build-matrix.json`; free-form release/base combinations are invalid |
| `profile` | `core` or `deployment` |
| `image` | One resolved leaf, or `all` |
| `dry_run` | Boolean; defaults to `true` |
| `approval` | Ignored for a dry run; must exactly match the frozen plan for publication |

Keep `dry_run: true` for routine planning:

```bash
gh workflow run publish.yml \
  --ref main \
  --field stream=2025.1-rocky-9 \
  --field profile=core \
  --field image=keystone \
  --field dry_run=true
```

The `publish-plan` job validates the repository configuration, renders one
frozen publish plan, and uploads `artifacts/plan/publish-plan.json` as
`publish-plan-<candidate-id>`. With `dry_run: true`, no authorization, registry
login, build, push, manifest, or lock-generation job runs.

## Publication approval

Only three non-dry-run scopes exist. The frozen publish plan supplies their
required repository variable, resolved count, and exact approval phrase:

| Scope | Required repository variable |
| --- | --- |
| `core/keystone` | `ALLOW_GHCR_PUBLISH=true` |
| `core/all` | `ALLOW_GHCR_FULL_CORE_PUBLISH=true` |
| `deployment/all` | `ALLOW_GHCR_DEPLOYMENT_PUBLISH=true` |

For the standing stream, the three exact phrases are:

```text
PUBLISH ghcr.io/supergate-hub/kolla-container-images 2025.1-rocky-9 core/keystone (1 image, amd64/arm64)
PUBLISH ghcr.io/supergate-hub/kolla-container-images 2025.1-rocky-9 core/all (21 images, amd64/arm64)
PUBLISH ghcr.io/supergate-hub/kolla-container-images 2025.1-rocky-9 deployment/all (63 images, amd64/arm64)
```

Counts vary by resolved stream, so operators must copy the count-bearing
phrase from that run's frozen publish plan instead of adapting an example. All
other partial-image scopes remain dry-run only. An approval for one stream or
scope cannot authorize another.

Every `dry_run: false` run also crosses the protected GitHub environment
`ghcr-publish`, where required reviewers and branch/tag restrictions are
configured manually. `authorize-publish` validates the three repository
variables and exact phrase against the frozen plan. Every parent/leaf build
unit and `finalize-publish` revalidates that approval before registry login.
Planning, authorization, and evidence aggregation remain read-only.

## Publication sequence

Writers for the same stream use `kolla-publish-<stream>` concurrency and do not
cancel an in-progress run. An approved publication follows this staged DAG:

```text
parent tier 0 -> parent tier 1 -> parent tier 2
              -> leaf stage 0 -> optional leaf stage 1
              -> aggregate native evidence
              -> manifests -> publish summary -> generic candidate lock
              -> hand off to openstack-infra-ops
```

1. `publish-plan` freezes and uploads the validated plan.
2. `authorize-publish` crosses `ghcr-publish` and binds approval to that plan.
3. `build-parent-tier-0`, `build-parent-tier-1`, and `build-parent-tier-2`
   run in order. Every matrix entry builds exactly one parent for one native
   architecture.
4. `build-leaf-stage-0` builds one selected leaf per native job from the raw
   immutable unit evidence produced by the parent tiers.
5. Optional `build-leaf-stage-1` runs only when a selected leaf depends on
   another selected leaf. Today that is the deployment chain
   `ovn-sb-db-server -> ovn-sb-db-relay`; stage 1 consumes the server unit's
   immutable digest from stage 0.
6. `collect-native-evidence` validates the exact parent-and-leaf closure and
   produces the aggregate
   AMD64 and ARM64 evidence documents.
7. `finalize-publish` downloads those exact evidence sets, creates each
   multi-architecture manifest from recorded child digests, validates the
   manifest and summary, and generates a lock only for `deployment/all`.

There is no `collect-parent-evidence` job and no parent-index artifact. Each
dependent job downloads the preceding raw unit JSON evidence directly, and
the final native aggregation verifies the complete frozen-plan closure.

Every build matrix uses `max-parallel: 4`. The frozen command for every
unit contains one anchored target, `--skip-existing`, `--threads 1`, and
`--push-threads 1`. A child job pulls its planned ancestors by immutable digest,
validates their native platform, and applies the candidate tags locally before
Kolla runs. The current Kolla summary must report only that target as built
and exactly its ancestor chain as skipped.

This public repository uses only the standard native GitHub-hosted runners:

```text
AMD64: ubuntu-24.04     (x86_64, linux/amd64)
ARM64: ubuntu-24.04-arm (aarch64, linux/arm64)
```

Larger runners and privately managed runner fleets are outside this design.
Each hosted VM is fresh: the job verifies its native machine and local Linux
Docker Unix socket, prunes Docker, installs the pinned Python packages without
a pip cache, and creates a job-scoped `DOCKER_CONFIG` below `RUNNER_TEMP`.

The standard hosted runner has 14 GB of SSD storage. A unit fails before build
when free Docker storage after cleanup is below 8 GiB, and disk sampling fails
the unit if the observed minimum during or immediately after build is below
2 GiB. The free hosted-only approach remains a feasibility gate until the
Keystone chain `base -> openstack-base -> keystone-base -> keystone` succeeds
as eight independent units: four targets on each native architecture.

Do not use **Re-run failed jobs**. Candidate identity includes the run attempt,
so a partial rerun cannot reuse a coherent upstream artifact set and fails
closed. Recovery is **Re-run all jobs**, which rebuilds every unit under a new
candidate ID.

Runner capacity and evidence requirements are detailed in
[build-readiness.md](build-readiness.md).

## Artifacts and validation

Artifact names and terminal paths are deterministic:

| Artifact | Contents |
| --- | --- |
| `publish-plan-<candidate-id>` | `artifacts/plan/publish-plan.json` |
| `unit-evidence-<arch>-<kind>-<target>-<candidate-id>` | One parent or leaf unit evidence JSON |
| `native-amd64-<candidate-id>` | `artifacts/arch/native-amd64.json` |
| `native-arm64-<candidate-id>` | `artifacts/arch/native-arm64.json` |
| `unit-diagnostics-<arch>-<kind>-<target>-<candidate-id>` | Text diagnostics uploaded only when that unit fails |
| `publish-<stream>-<candidate-id>` | `artifacts/publish-summary-<stream>.json`, `artifacts/manifests/`, and the candidate lock when eligible |

The publish plan, successful unit evidence, aggregate native evidence, and
terminal publish artifact are retained for **seven days**. A failed unit's
short text diagnostic is retained for **one day**. Seven days safely covers a
large sharded run that queues behind `max-parallel: 4`; the evidence is small
JSON, not image data. The workflow never uploads image tar files, Docker
layers, or Docker/cache directories as artifacts. All upload and download
names include the candidate ID; the architecture aggregate pattern is
`native-<arch>-<candidate-id>`.

The publish summary covers resolved deployable leaves only. Finalization
allows exactly the standard OCI image-index media type
`application/vnd.oci.image.index.v1+json` or Docker manifest-list media type
`application/vnd.docker.distribution.manifest.list.v2+json`. Each manifest
must contain exactly `linux/amd64` and `linux/arm64`, and its descriptor child
digests must equal the immutable digests in the two native evidence artifacts.
It also validates the metadata descriptor's digest/media type/size, requires
the raw manifest media type to match that descriptor, hashes the raw
`repository@digest` response, and requires the mutable deploy tag to return
the same bytes before writing the summary. The optional terminal lock path is:

```text
artifacts/kolla-ansible-image-lock-<stream>.yml
```

Only `deployment/all` may produce that generic candidate lock. A core,
Keystone, partial deployment, incomplete evidence set, or invalid publish
summary cannot produce one.

`publish-<stream>-<candidate-id>` is uploaded as the complete candidate
artifact before any stream alias changes. A partial stream-alias failure fails
the workflow but cannot invalidate that candidate lock. Recovery uses
**Re-run all jobs**, creating a new candidate ID; aliases are non-transactional
convenience references and are never lock inputs.

## Kolla-Ansible multi-architecture consumption

Operations set registry and stream defaults in `globals.yml` without an
architecture suffix:

```yaml
# globals.yml
docker_registry: ghcr.io
docker_namespace: supergate-hub/kolla-container-images
docker_registry_insecure: "no"

openstack_release: "2025.1"
kolla_base_distro: rocky
kolla_base_distro_version: "9"
openstack_tag_suffix: ""
```

The generated candidate lock is supplied alongside `globals.yml` as an
operations-managed `globals.d` file or explicit extra-vars file:

For candidate ID `123456789-1`, the corresponding image references are:

```text
AMD64 child:
ghcr.io/supergate-hub/kolla-container-images/nova-compute:2025.1-rocky-9-candidate-123456789-1-amd64
ARM64 child:
ghcr.io/supergate-hub/kolla-container-images/nova-compute:2025.1-rocky-9-candidate-123456789-1-arm64
Candidate multi-architecture ref used by the lock:
ghcr.io/supergate-hub/kolla-container-images/nova-compute:2025.1-rocky-9-candidate-123456789-1
Convenience stream alias, not used by the lock:
ghcr.io/supergate-hub/kolla-container-images/nova-compute:2025.1-rocky-9
```

```yaml
# generated candidate lock supplied as an extra-vars file
_kolla_candidate_lock:
  # Digest-bound supply evidence; Kolla-Ansible roles ignore this reserved data.
  images:
    "nova-compute":
      deploy_ref: "ghcr.io/supergate-hub/kolla-container-images/nova-compute:2025.1-rocky-9-candidate-123456789-1"
      manifest_digest: "sha256:<multi-arch-manifest-digest>"
      immutable_ref: "ghcr.io/supergate-hub/kolla-container-images/nova-compute@sha256:<multi-arch-manifest-digest>"
nova_compute_image_full: "ghcr.io/supergate-hub/kolla-container-images/nova-compute:2025.1-rocky-9-candidate-123456789-1"
```

Before deployment, openstack-infra-ops resolves every deploy_ref, compares its
manifest bytes and digest with manifest_digest and immutable_ref, and only then
passes the root-level variables to Kolla-Ansible. The pinned Kolla-Ansible
releases do not enforce digest identity themselves; a successful extra-vars
load is not a substitute for this verification.

Operators do not select `-amd64` or `-arm64` image tags in Kolla-Ansible.
Docker or Podman automatically chooses the matching child from the
multi-architecture manifest on both homogeneous and mixed-architecture
clusters. Because the GHCR packages are Public, Kolla hosts pull them
anonymously and do not receive a GitHub username, PAT, or `read:packages`
credential.

## Manual GitHub and GHCR prerequisites

Package는 첫 push 전에 존재하지 않을 수 있다. 따라서 repository, branch,
environment, variable은 canary 전에 준비하고, package 연결과 Public 전환은
첫 canary push 후에 수행한다.

### Pre-canary: 첫 push 전

1. Repository visibility를 **Public**로 유지하고 Actions의 standard
   `ubuntu-24.04` 및 `ubuntu-24.04-arm` runner 사용을 허용한다. repository는
   하나만 사용하고 larger runner나 별도 privately managed build pool은 구성하지
   않는다. Organization `Settings -> Packages -> Package Creation`에서도 Public
   package 생성을 허용한다. Workflow는 repository `GITHUB_TOKEN`으로 생성한
   package가 repository의 visibility/permission model을 상속하는 GitHub 기본값을
   사용하지만, granular GHCR visibility를 추정하지 말고 canary 뒤 실제 값이
   Public인지 확인한다.
2. `Settings -> Branches` 또는 `Settings -> Rules -> Rulesets`에서 `main`을
   보호한다. 정확히 다음을 선택한다: pull request 필수, 승인 최소
   1명, required status check
   `Validate repository configuration and publish plans`, conversation 해결 필수,
   force push 금지, branch 삭제 금지, bypass 대상 없음. 실제 publish 코드는
   `github.ref == refs/heads/main` 및 `github.ref_protected == true`를 다시 검사하므로,
   이 보호가 없으면 package push를 시작할 수 없다.
3. `Settings -> Environments`에서 `ghcr-publish`를 만든다. required
   reviewer를 지정하고 deployment branches/tags는 selected branches and tags의
   `main`만 허용한다. 특정 개인을 임의로 추측해 지정하지 말고 조직의
   실제 publish 승인자를 선택한다.
4. `Settings -> Secrets and variables -> Actions -> Variables`에
   `ALLOW_GHCR_PUBLISH`, `ALLOW_GHCR_FULL_CORE_PUBLISH`,
   `ALLOW_GHCR_DEPLOYMENT_PUBLISH`를 만든다. 평소에는 모두 `false`로 두고,
   승인된 공개 창에서 필요한 scope 하나만 `true`로 전환한다.
5. `Settings -> Actions -> General -> Workflow permissions`의 default를
   **Read repository contents permission**으로 설정하고 Actions의 PR 생성/승인 허용은
   끄는다. Workflow 전체에 write를 주지 않고, 네이티브 parent/leaf build unit과
   `finalize-publish` job에만 job-scoped `packages: write`를 사용한다. 외부 CI가
   dispatch한다면 repository-scoped GitHub App 또는 동등한 단기 credential에
   `Actions: write`만 주고 no package-write permission을 유지한다.
6. Feature branch에서 `dry_run: true`를 dispatch해 먼저 검증한다. Dry-run은
   branch protection이 없는 feature branch에서도 허용되지만 registry를 변경하지 않는다.
   생성된 `publish-plan-<candidate-id>`에서 Keystone가 양 architecture 합계
   8개 unit인지와 exact count-bearing approval phrase를 확인한 뒤 보호된
   `main`으로 PR을 merge한다.

   ```bash
   feature_branch="$(git branch --show-current)"
   gh workflow run publish.yml \
     --ref "$feature_branch" \
     --field stream=2025.1-rocky-9 \
     --field profile=core \
     --field image=keystone \
     --field dry_run=true
   ```

### First canary: 보호된 main에서

7. `ALLOW_GHCR_PUBLISH=true`만 활성화하고 `core/keystone`를
   `dry_run: false`, `--ref main`, dry-run plan의 exact approval phrase로 별도
   dispatch한다. 실행 내역에서 standard hosted label
   `ubuntu-24.04`/`ubuntu-24.04-arm`, 정확히 8개 unit, 각 unit의 8 GiB
   preflight와 관측된 2 GiB minimum, 양 architecture native evidence,
   Keystone의 exact two-platform multi-architecture manifest와 publish summary를
   확인한다. 14 GB runner에서 one-target sharding이 실제로 충분한지는 이
   canary가 확정한다. 현재 문서는 이 canary가 실행되었다고 주장하지 않는다.
   `ghcr-publish` 승인은 plan 생성 후 48시간 안에 완료한다. 그 안에 승인하지
   못하면 실행을 취소하고 새 workflow run과 새 candidate ID로 다시 시작한다.
   7일 plan 보존 기간의 후반에 오래된 실행을 승인하거나 개별 job을 rerun하지
   않는다.

   ```bash
   gh workflow run publish.yml \
     --ref main \
     --field stream=2025.1-rocky-9 \
     --field profile=core \
     --field image=keystone \
     --field dry_run=false \
     --field 'approval=PUBLISH ghcr.io/supergate-hub/kolla-container-images 2025.1-rocky-9 core/keystone (1 image, amd64/arm64)'
   ```

### Post-first-canary: package가 생긴 후

8. Organization의 `Packages`에서 새 parent/leaf package가 이 repository와
   연결(link)되었는지 확인하고, 각 package visibility를 **Public**으로
   명시적으로 변경한다. GitHub UI/문서의 경고대로 Public 전환은 되돌릴 수 없는
   작업으로 취급하고, 이미지에 secret이나 site-specific configuration이 없음을
   먼저 확인한다. 로그아웃한 client 또는 빈 Docker config에서 unauthenticated
   manifest inspection과 pull을 테스트한다:

   ```bash
   docker logout ghcr.io || true
   anonymous_docker_config="$(mktemp -d)"
   DOCKER_CONFIG="$anonymous_docker_config" docker buildx imagetools inspect \
     ghcr.io/supergate-hub/kolla-container-images/keystone:<candidate-tag>
   DOCKER_CONFIG="$anonymous_docker_config" docker pull \
     ghcr.io/supergate-hub/kolla-container-images/keystone:<candidate-tag>
   rm -rf -- "$anonymous_docker_config"
   ```

   익명 pull이 성공해야 canary를 완료로 판정한다. 이후 `core/all` 또는
   `deployment/all`을 게시하면 그 실행에서 처음 생긴 모든 parent/leaf package에도
   같은 link 확인, Public 전환, 익명 pull 검증을 반복한다. 특히 deployable leaf
   package 전체가 Public인지 확인하기 전에는 full publish를 Public 서빙 완료로
   판정하지 않는다. 그런 뒤 variable을 다시
   `false`로 복구하고 vulnerability scanning과 GHCR cleanup policy를 적용한다.
   Actions의 성공 JSON evidence/terminal artifact retention은 7일, failure diagnostics는
   1일을 유지하며 Docker layer, image tar, cache artifact는 추가하지 않는다.

첫 push 전에는 Organization `Packages`에 같은 이름의 기존 package가 있는지도
확인한다. 이미 존재하면서 이 repository에 연결되지 않은 package는 현재 workflow의
`GITHUB_TOKEN` 쓰기 권한을 상속하지 않을 수 있으므로, 임의로 우회하지 말고 package
소유자/관리자가 repository 연결과 Actions access를 바로잡은 뒤 새 run을 시작한다.

이 체크리스트 자체는 현재 GitHub 설정의 존재를 증명하지 않는다. 각 publish
직전에 branch protection, environment reviewer, repository variable, package
visibility/linkage를 GitHub에서 다시 조회하고 실제 관측 결과를 run 기록에 남긴다.

## Handoff and secret boundary

The validated publish summary and, only for `deployment/all`, the generic
candidate lock are this repository's terminal outputs. `openstack-infra-ops`
or another dedicated external deployment/promotion system reviews and copies
the lock, creates environment-specific locks and pointers, performs
matching-OS deployment smoke, and owns promotion, site deployment, and
rollback.

This repository stops at that handoff. It does not create Dev/Stg/Prod locks,
tags, pointers, promotions, deployments, or rollback actions. Public GHCR
consumption changes only registry authentication: consumers may pull the
multi-architecture image anonymously; it does not move environment ownership
into this repository.

Registry credentials, OpenStack credentials, Ceph keys, private CAs, and
site-specific configuration remain in external secret/configuration domains
and are never embedded in images or generated candidate locks.
