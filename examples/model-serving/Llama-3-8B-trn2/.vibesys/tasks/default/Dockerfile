# Native TorchNeuron under VibeSys. The TorchNeuron DLC (the trainium backend's
# default image) installs torch-neuronx and torchtitan-neuron in editable mode
# from /workspace, which VibeSys replaces with the candidate project at run time,
# so `import torch` fails inside the run. Move both sources out of /workspace and
# repoint their editable finders. Copy this file into any trainium task.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
RUN set -eux; \
    site="$(python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"; \
    mkdir -p /opt/neuron-src; \
    for pkg in torch_neuronx torchtitan_neuron; do \
      if [ -d "/workspace/$pkg" ]; then \
        mv "/workspace/$pkg" "/opt/neuron-src/$pkg"; \
        grep -rl "/workspace/$pkg" "$site"/__editable__* | xargs -r sed -i "s#/workspace/$pkg#/opt/neuron-src/$pkg#g"; \
      fi; \
    done; \
    ! grep -rq "/workspace/" "$site"/__editable__*
