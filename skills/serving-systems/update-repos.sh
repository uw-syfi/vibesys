#!/bin/bash
# Sparse-checkout reference serving engines into repos/.
# Usage: bash update-repos.sh [vllm|sglang|trtllm|all]

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPOS_DIR="$SCRIPT_DIR/repos"

clone_or_update() {
    local name="$1"
    local url="$2"
    local branch="$3"
    shift 3
    local sparse_dirs=("$@")

    local repo_dir="$REPOS_DIR/$name"
    mkdir -p "$REPOS_DIR"

    echo ""
    echo "=== $name ==="

    if [ -d "$repo_dir/.git" ]; then
        echo "  updating..."
        cd "$repo_dir"
        git pull --ff-only origin "$branch" 2>/dev/null || git pull origin "$branch"
    else
        echo "  sparse-checkout clone..."
        git clone --filter=blob:none --no-checkout --depth 1 --branch "$branch" "$url" "$repo_dir"
        cd "$repo_dir"
        git sparse-checkout init --cone
        git sparse-checkout set "${sparse_dirs[@]}"
        git checkout "$branch"
    fi

    du -sh "$repo_dir" 2>/dev/null | awk '{print "  size: "$1}'
}

vllm_dirs=(
    "vllm"
    "csrc"
    "examples"
    "benchmarks"
    "tests"
    "docs"
)

sglang_dirs=(
    "python/sglang/srt"
    "python/sglang/jit_kernel"
    "python/sglang/lang"
    "sgl-kernel/csrc"
    "sgl-kernel/include"
    "sgl-kernel/python"
    "examples"
    "benchmark"
    "docs"
)

trtllm_dirs=(
    "tensorrt_llm"
    "cpp/tensorrt_llm"
    "examples"
    "triton_backend"
    "benchmarks"
    "docs"
)

TARGET="${1:-all}"

case "$TARGET" in
    vllm)
        clone_or_update "vllm" "https://github.com/vllm-project/vllm.git" "main" "${vllm_dirs[@]}"
        ;;
    sglang)
        clone_or_update "sglang" "https://github.com/sgl-project/sglang.git" "main" "${sglang_dirs[@]}"
        ;;
    trtllm)
        clone_or_update "TensorRT-LLM" "https://github.com/NVIDIA/TensorRT-LLM.git" "main" "${trtllm_dirs[@]}"
        ;;
    all)
        clone_or_update "vllm" "https://github.com/vllm-project/vllm.git" "main" "${vllm_dirs[@]}"
        clone_or_update "sglang" "https://github.com/sgl-project/sglang.git" "main" "${sglang_dirs[@]}"
        clone_or_update "TensorRT-LLM" "https://github.com/NVIDIA/TensorRT-LLM.git" "main" "${trtllm_dirs[@]}"
        ;;
    *)
        echo "usage: bash update-repos.sh [vllm|sglang|trtllm|all]"
        exit 1
        ;;
esac

echo ""
echo "=== summary ==="
du -sh "$REPOS_DIR"/*/ 2>/dev/null || echo "  (no repos yet)"
