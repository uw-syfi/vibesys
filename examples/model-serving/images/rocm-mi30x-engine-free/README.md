# Engine-free ROCm MI300A image

Container recipe for agents that build their own serving engine on AMD MI300A
(gfx942). It is `lmsysorg/sglang:v0.5.18-rocm700-mi30x` with the engines
removed, so no baseline engine can be imported or copied.

## Removed and kept

| Removed | Why |
| --- | --- |
| `sglang`, `sgl-kernel`, `sglang-router`/`sgl-router`, `/sgl-workspace/sglang` | the base image's engine |
| `vllm`, `tensorrt_llm` | other engines, if present |
| editable installs, stray dist-info, `.pth`/egg-link, console scripts | leftovers that keep `import` or CLI working |

Kept: torch (ROCm build), triton, AITER (`amd-aiter`, kernels only, with its
JIT toolchain), the ROCm compilers, transformers, tokenizers, safetensors,
huggingface_hub, aiohttp, fastapi, uvicorn, numpy, pydantic, uvloop, orjson.
These are what a hand-written engine needs.

Only the engine packages are named in `pip uninstall -y`; there is no
autoremove, so shared dependencies are not pulled out. `verify_image.py` runs
as a build step and fails the build if an engine still imports, a kept package
fails to import, or `torch.version.hip` is `None`. Run it again in the
container: `python3 /opt/verify_image.py`.

## Build

```bash
docker build -t rocm-mi30x-engine-free:v0.5.18 examples/model-serving/images/rocm-mi30x-engine-free
```

## Stage for Slurm with pyxis/enroot

```bash
# on a build host with docker and enroot
enroot import -o <SQSH_PATH> dockerd://rocm-mi30x-engine-free:v0.5.18
# or, from a registry: enroot import -o <SQSH_PATH> docker://<REGISTRY>/<IMAGE>:<TAG>
```

Put the `.sqsh` on storage visible from the compute nodes. Then write an
environment definition file (EDF) pointing at it. Generic example:

```toml
image = "<SQSH_PATH>"
mounts = ["<SCRATCH>:<SCRATCH>", "<MODEL_DIR>:<MODEL_DIR>"]
workdir = "<SCRATCH>"
writable = true
```

Placeholders are yours to fill. Field names follow the pyxis/enroot EDF
convention; check your site docs.

The operator's `clusters.toml` profile selects the EDF through
`command_prefix`:

```toml
command_prefix = ["srun", "--overlap", "--environment=<EDF_PATH>"]
```

See [remote Slurm execution](../../../../docs/contributing/remote-slurm-execution.md)
for `clusters.toml` and `~/.sky`. This recipe is site-config-free: usernames,
paths, accounts, partitions, and cluster names stay in the operator's
`clusters.toml` and `~/.sky`.

## Verification status

The image was not built in the authoring environment. Docker and network were
available (the base tag's manifest resolves), but the base image is tens of GB
and no ROCm GPU was present. Not verified: the actual uninstall result, the
exact editable/console-script paths in the base image, and the `verify_image.py`
build step. The Dockerfile and README are checked statically by
`tests/examples/test_engine_free_image.py`. Expect to adjust cleanup paths on
the first real build; the verification step flags any leftover.
