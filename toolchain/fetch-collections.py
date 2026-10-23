"""Fetch the pinned Ansible collections for the toolchain image build.

Galaxy artifacts are downloaded and checked against their recorded SHA-256.
openstack.kolla is fetched at its exact commit and built into a collection
artifact. The image stage then installs only these local artifacts, offline.
"""

import hashlib
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path


def download(url: str) -> bytes:
    for delay in (5, 15, 30, None):
        try:
            with urllib.request.urlopen(url, timeout=60) as response:
                return response.read()
        except OSError:
            if delay is None:
                raise
            time.sleep(delay)
    raise AssertionError("unreachable")


def main(manifest_path: str, output: str) -> int:
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    target = Path(output)
    target.mkdir(parents=True, exist_ok=False)
    for collection in manifest["collections"]:
        data = download(collection["url"])
        digest = hashlib.sha256(data).hexdigest()
        if digest != collection["sha256"]:
            print(f"{collection['name']}: sha256 {digest} != {collection['sha256']}", file=sys.stderr)
            return 1
        (target / collection["url"].rsplit("/", 1)[1]).write_bytes(data)

    kolla = manifest["kolla_collection"]
    source = Path("/tmp/openstack-kolla")
    subprocess.run(["git", "init", "--quiet", str(source)], check=True)
    for delay in (5, 15, 30, None):
        fetch = subprocess.run(
            ["git", "-C", str(source), "fetch", "--quiet", "--no-tags", "--depth=1", kolla["repository"], kolla["commit"]]
        )
        if fetch.returncode == 0:
            break
        if delay is None:
            return fetch.returncode
        time.sleep(delay)
    subprocess.run(["git", "-C", str(source), "checkout", "--quiet", "--detach", "FETCH_HEAD"], check=True)
    head = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    if head != kolla["commit"]:
        print(f"openstack.kolla: checked out {head}, expected {kolla['commit']}", file=sys.stderr)
        return 1
    subprocess.run(
        ["ansible-galaxy", "collection", "build", "--output-path", str(target), str(source)], check=True
    )
    built = target / f"openstack-kolla-{kolla['galaxy_version']}.tar.gz"
    if not built.is_file():
        print(f"openstack.kolla: {built.name} was not built", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(*sys.argv[1:]))
