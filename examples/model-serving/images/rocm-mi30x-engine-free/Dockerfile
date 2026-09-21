# Engine-free ROCm MI300A (gfx942) image: the SGLang ROCm base with the serving
# engines removed, so an agent builds its own engine on torch/triton/AITER.
FROM lmsysorg/sglang:v0.5.18-rocm700-mi30x

# Remove engines only. Plain `pip uninstall -y` on named packages; never
# autoremove, which could drag out shared dependencies (torch, triton, ...).
# `|| true` tolerates absent packages; verify_image.py is the real gate.
RUN set -eux; \
    for pkg in sglang sgl-kernel sglang-kernel sglang-router sgl-router \
               vllm tensorrt_llm tensorrt-llm; do \
        pip uninstall -y "$pkg" || true; \
    done; \
    rm -rf /sgl-workspace/sglang /sgl-workspace/sgl-kernel /sgl-workspace/vllm; \
    SP="$(python3 -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"; \
    rm -rf "$SP"/sglang* "$SP"/sgl_kernel* "$SP"/sgl_router* "$SP"/sglang_router* \
           "$SP"/vllm* "$SP"/tensorrt_llm* \
           "$SP"/__editable__*sglang* "$SP"/__editable__*vllm* "$SP"/__editable__*tensorrt* \
           "$SP"/*sglang*.egg-link "$SP"/*sglang*.pth "$SP"/*vllm*.egg-link "$SP"/*vllm*.pth; \
    for bin in sglang sglang-router sgl-router vllm trtllm-serve trtllm-build trtllm-bench; do \
        rm -f "/usr/local/bin/$bin" "/opt/venv/bin/$bin"; \
    done; \
    pip cache purge || true

COPY verify_image.py /opt/verify_image.py

# Fail the build if an engine is still importable, a kept package broke, or
# torch is not a ROCm build.
RUN python3 /opt/verify_image.py
