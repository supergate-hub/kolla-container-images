# Kolla-Ansible toolchain image

The toolchain image is the control-node side of a Kolla-Ansible release: one
exact Kolla-Ansible version, a hashed Python environment and pinned Ansible
collections, published as
`ghcr.io/supergate-hub/kolla-container-images/kolla-ansible`. A consumer runs it
as a container (for example a Kubernetes Job) with its inventory,
configuration, passwords and SSH key mounted at run time; none of those are in
the image.

It has its own workflow because it depends only on the Kolla-Ansible toolchain,
not on a stream: every stream of a release uses the same control node, and the
service-image publish pipeline stays unchanged.

## Pins

Each toolchain has a reviewed directory `toolchain/<version>/`:

| File | Contents |
| --- | --- |
| `toolchain.json` | Release, base, Kolla-Ansible version and commit, Python inputs, upper-constraints digest, Galaxy collections and `openstack.kolla` |
| `requirements.lock` | Every Python distribution, exact and hashed for amd64 and arm64 wheels |

`scripts/toolchain_image.py validate` fails closed unless:

- the Kolla-Ansible commit equals the toolchain in `config/build-matrix.json`;
- an enabled stream joins the toolchain to the release;
- the base is an Ubuntu base from the matrix, since the Dockerfile uses apt;
- the upper-constraints digest equals the release source-set's `openstack/requirements` record;
- every lock entry is `name==version` with SHA-256 hashes, kolla-ansible is the toolchain version, and pip itself is absent;
- each collection version satisfies the constraint from Kolla-Ansible's `requirements-core.yml` (or of the collection that requires it) and is fetched from its Galaxy artifact URL by SHA-256;
- `openstack.kolla` is the release recorded in the matrix-pinned `openstack/releases` commit, at its exact commit.

Pins change only by pull request. To create or refresh them for a toolchain,
run the manual maintenance tool, which needs network access and `uv`:

```bash
python3 scripts/generate-toolchain-pins.py --toolchain 22.2.0
```

It downloads upper-constraints from the source-set commit and verifies its
digest, picks the newest stable Galaxy release inside each constraint
(following collection dependencies), takes `openstack.kolla` from the release
metadata, and resolves the Python lock for both architectures, refusing to
write when the two resolutions differ.

## Image contents

`toolchain/Dockerfile` builds from the base child digest frozen in the plan:

1. apt installs only `ca-certificates`, `openssh-client`, `python3` and `python3-venv`.
2. pip installs `requirements.lock` with `--require-hashes --no-deps --only-binary=:all:`, then pip is removed from the environment.
3. A throwaway stage downloads the Galaxy artifacts, checks their SHA-256, fetches `openstack.kolla` at its commit and builds its artifact.
4. The final stage installs those artifacts with `ansible-galaxy collection install --offline --no-deps` into `/usr/share/ansible/collections` and runs as UID 42400.

## Workflow

`toolchain.yml` takes two inputs: `operation` (`plan` or `publish`) and
`toolchain` (one choice per `toolchain/<version>/`, checked by tests).

| Job | Does |
| --- | --- |
| `toolchain-plan` | Requires repository-owned `main`, validates the pins, resolves the base index and per-architecture children, and uploads the plan |
| `authorize-toolchain` | `publish` only: crosses the `ghcr-publish` environment and requires protected `main` |
| `build-native` | Builds on native `ubuntu-24.04` and `ubuntu-24.04-arm` runners, refuses an existing revision tag, pushes, pulls the pushed digest, smoke-checks it, then records evidence |
| `finalize-toolchain` | Creates the revision manifest from the two digests, rereads it, and uploads the toolchain lock |

The smoke check runs the image with networking disabled. It compares the
installed Python distributions with the lock exactly, proves the kolla-ansible
package was built from the pinned commit (its `pbr.json`), checks that
`kolla-ansible --version` and `openstack --version` report the pinned
versions, lists the installed collections against the pins, and refuses root.

Tags follow `<toolchain>-<distro>-<os_version>-rev-<run id>-<attempt>`, with
`-amd64` and `-arm64` children. There is no moving alias; consumers use the
digest from the lock.

`toolchain-check.yml` builds every toolchain for amd64 on pull requests that
change it and runs the same smoke check, without pushing.

## Handoff

The artifact `toolchain-<version>-<candidate>` holds
`kolla-ansible-toolchain-lock-<version>.json` and the raw manifest bytes, and is
retained for 90 days. The lock (schema 1) records:

- the revision ref, manifest digest, immutable ref and both architecture children;
- the Kolla-Ansible version and commit;
- the upper-constraints digest, the requirements lock digest and every package version;
- the collections and `openstack.kolla` pins;
- the frozen base;
- the digests of the toolchain inputs and the source commit.

`openstack-infra-ops` copies the lock before it expires, verifies the digest
in its own registry, and runs the image against its environments. This
repository does not.
