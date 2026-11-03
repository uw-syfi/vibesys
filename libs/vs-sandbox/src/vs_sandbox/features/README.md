# Dev Container Features

Each Feature belongs to the module that owns what it installs. The agent layer
lives in `vs-agent`, the toolchain catalog here, and run environment extras in
`vs-runtime`. Each owner passes its Feature folders to the image builder as
data; the builder does not import the owners.

A Feature project has `src/<id>/devcontainer-feature.json` and `install.sh`,
with `test/<id>/test.sh` for the Dev Container CLI. VibeSys generates a
Dockerfile from the ordered Features and builds it with Docker. CI also tests
Features with the Dev Container CLI against supported base images. The
`change-container-images` skill describes how to change them.
