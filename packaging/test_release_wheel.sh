#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
    echo "usage: $0 <wheel>" >&2
    exit 2
fi

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
wheel=$(realpath "$1")

if [ ! -f "$wheel" ]; then
    echo "release wheel does not exist: $wheel" >&2
    exit 2
fi

context=$(mktemp -d "${TMPDIR:-/tmp}/vibesys-release-wheel.XXXXXX")
image_id=""
cleanup() {
    if [ -n "$image_id" ]; then
        docker image rm "$image_id" >/dev/null 2>&1 || true
    fi
    rm -rf "$context"
}
trap cleanup EXIT HUP INT TERM

cp "$wheel" "$context/$(basename "$wheel")"
cp "$script_dir/verify_installed_release.py" "$context/verify_installed_release.py"
cp "$script_dir/release-wheel.Dockerfile" "$context/Dockerfile"

image_id=$(docker build --quiet "$context")
docker run --rm "$image_id"
