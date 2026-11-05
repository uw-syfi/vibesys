# The task image: the vibesys-evaluator llm-serving image (the package, and the AgentX harness at
# the version the package replays with) plus this directory. It runs bench.py; it has no GPU
# and reaches the candidate's server only over HTTP (VIBESYS_SERVICE_URL).
#
# The base names its version tag only: 0.1.0rc3 is the first release with this image, and its
# digest exists once the release publishes it (.github/workflows/evaluator-release.yml records
# it in the release notes). Add `@sha256:<digest>` from those notes when it is released; until
# then this file builds only where that tag has been built locally (docker build --target
# llm-serving -t ghcr.io/uw-syfi/vibesys-evaluator-llm-serving:0.1.0rc3 -f
# sdk/vibesys-evaluator/image/Dockerfile sdk/vibesys-evaluator).
FROM ghcr.io/uw-syfi/vibesys-evaluator-llm-serving:0.1.0rc3

COPY . /task
