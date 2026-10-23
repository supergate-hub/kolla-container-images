# Kolla-Ansible toolchain image: one exact Kolla-Ansible release, its hashed
# Python environment and pinned Ansible collections (toolchain/<version>/).
# .github/workflows/toolchain.yml builds it natively per architecture from the
# base child digest frozen in the plan. Inventories, configuration, passwords
# and keys are never part of the image; the consumer mounts them at run time.
ARG BASE_IMAGE

FROM ${BASE_IMAGE} AS runtime
ARG TOOLCHAIN
ENV DEBIAN_FRONTEND=noninteractive \
    PATH=/opt/kolla-ansible/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    PYTHONDONTWRITEBYTECODE=1
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates openssh-client python3 python3-venv \
 && rm -rf /var/lib/apt/lists/*
COPY ${TOOLCHAIN}/requirements.lock /opt/kolla-ansible/requirements.lock
RUN python3 -m venv /opt/kolla-ansible/venv \
 && /opt/kolla-ansible/venv/bin/python -m pip install --no-cache-dir --disable-pip-version-check \
      --no-deps --only-binary=:all: --require-hashes -r /opt/kolla-ansible/requirements.lock \
 && /opt/kolla-ansible/venv/bin/python -m pip uninstall --yes --disable-pip-version-check pip

FROM runtime AS collections
ARG TOOLCHAIN
RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*
COPY ${TOOLCHAIN}/toolchain.json /tmp/toolchain.json
COPY fetch-collections.py /tmp/fetch-collections.py
RUN python3 /tmp/fetch-collections.py /tmp/toolchain.json /tmp/collections

FROM runtime
ARG TOOLCHAIN
ARG SOURCE_COMMIT=unknown
COPY --from=collections /tmp/collections /tmp/collections
RUN ansible-galaxy collection install --offline --no-deps --collections-path /usr/share/ansible/collections \
      /tmp/collections/*.tar.gz \
 && rm -rf /tmp/collections \
 && useradd --create-home --uid 42400 --user-group --shell /bin/bash kolla
USER 42400:42400
WORKDIR /home/kolla
LABEL org.opencontainers.image.title="kolla-ansible" \
      org.opencontainers.image.version="${TOOLCHAIN}" \
      org.opencontainers.image.revision="${SOURCE_COMMIT}" \
      org.opencontainers.image.source="https://github.com/supergate-hub/kolla-container-images" \
      org.opencontainers.image.licenses="Apache-2.0"
