#!/usr/bin/env bash
# The only writer of CI base mirrors. Consumers read them with the job's token and
# fall back to the same digest upstream, so this is not on any build's critical path.
set -euo pipefail
printf '%s' "${GHCR_TOKEN:?registry token required}" |
    skopeo login ghcr.io --username "${GITHUB_ACTOR:?actor required}" --password-stdin
while read -r source tag mirror; do
    # Once a pin is mirrored, even this maintenance path needs no Docker Hub.
    if timeout --kill-after=5s 60s skopeo inspect --raw "docker://${mirror}" >/dev/null 2>&1; then
        continue
    fi
    # --all preserves platforms, and --preserve-digests rejects any conversion.
    # Only initial publication needs the upstream registry. Retry at most three
    # times with backoff.
    for attempt in 1 2 3; do
        if timeout --kill-after=5s 240s skopeo copy --all --preserve-digests \
            "docker://${source}" "docker://${tag}"; then
            break
        fi
        if [[ ${attempt} == 3 ]]; then exit 1; fi
        sleep "$((attempt * 5))"
    done
    # An authenticated read of the digest consumers use proves the package is pullable.
    timeout --kill-after=5s 60s skopeo inspect --raw "docker://${mirror}" >/dev/null
done < <(python3 -m scripts.ci_base_images)
