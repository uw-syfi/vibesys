# Keep the base's Ubuntu release and components; only the archive endpoint
# comes from the hosted runner. No apt update is needed to prepare this layer.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
ARG UBUNTU_MIRROR
RUN set -eu; \
    test -n "${UBUNTU_MIRROR}"; \
    find /etc/apt -type f \( -name '*.list' -o -name '*.sources' \) \
        -exec sed -i -E "s@https?://([a-zA-Z0-9-]+[.])?(archive[.]ubuntu[.]com|security[.]ubuntu[.]com)/ubuntu/?@${UBUNTU_MIRROR}/@g" {} +
