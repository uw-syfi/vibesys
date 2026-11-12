# The task image (schema 0.2): the judge. It runs the service checker and the AgentX replay
# against the candidate's server at VIBESYS_SERVICE_URL; it has no GPU and no candidate code.
# The candidate's server runs in its own image, built from the candidate's commit by the
# project's Dockerfile.rocm (vibesys.input.toml, [candidate]).
#
# The base is the vibesys-evaluator llm-serving image of release 0.2.0rc1, pinned by the digest
# its release notes record: the vibesys_evaluator package and agentx-harness 1.0.7, the
# harness version the earlier [evaluator] (vibesys-evaluator-agentx 0.1.0) locked, replayed
# with the same recipe. checker.py is a byte-for-byte copy of
# .vibesys/shared/accuracy_checker/checker.py, kept in this directory so that it is part
# of the task's version and of this image, never read from a candidate's checkout.
FROM ghcr.io/uw-syfi/vibesys-evaluator-llm-serving:0.2.0rc1@sha256:8650a93b953c6e7a46b0adcf29f7d0de7e61357e914a79f8413555b1b55f83d0

COPY . /task
# The operator's Hugging Face cache, mounted read-only as "hf" (the task's [environment] needs):
# the pinned tokenizer and the prepared AgentX corpus are read from it offline.
ENV HF_HOME=/vibesys/mounts/hf HF_HUB_OFFLINE=1
# The checker's one dependency (httpx) is in the base's locked environment.
RUN python -I -c "import httpx, vibesys_evaluator.llm_serving.agentx" \
 && python -I /task/checker.py --help >/dev/null
