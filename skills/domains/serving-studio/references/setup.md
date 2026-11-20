# Setting up ServingStudio on a host

Do this once per host, with the user's approval, before an optimization starts. An agent in the
middle of an optimization never installs, builds or upgrades ServingStudio: a missing simulator
is a fact to report, not a step to take.

ServingStudio publishes no Python package, container image or release tag. A commit is the only
stable name for a revision, and [`../assets/pins.toml`](../assets/pins.toml) holds the ones this
skill was written against. Its code is source-available under PolyForm licenses, not open
source: use it from its own repositories and never copy its code or skills into the repository
you optimize.

## Choose a mode, best first

1. **A ServingStudio API endpoint.** Agents need a URL and nothing else, and the service queues
   runs, so parallel agents do not oversubscribe the host. The hosted service at
   `https://servingstudio.cs.washington.edu/api/public/v1` is the default and serves published
   GPU presets. A team can run its own server from a checkout (ServingStudioSim's
   `public_api/README.md`, `python -m public_api serve`) with its own `profile.db`, and point
   `SERVINGSTUDIO_API` at it.
2. **A local ServingStudioSim checkout.** Needed for what the API does not serve: a deployment
   the checkout models but the API does not publish (Trainium today, on its pinned commit), and
   the launcher's what-if overrides. A checkout adds no model or hardware by itself: what it
   cannot simulate, it cannot simulate locally either. Build it
   once at a pinned commit and give agents its path as `SERVINGSTUDIO_SIM`.

## Build a local checkout

Needs Linux, git, uv, Python 3.12, a Rust toolchain, `just`, a C/C++ toolchain and `mold` on
x86-64 (ServingStudioSim's README lists them). No GPU is needed to simulate over the bundled
`profile.db`; filling missing kernel rows needs the hardware.

The pin is the task's when it names one, otherwise this skill's:

```toml
# .vibesys/tasks/<task>/vibesys.input.toml
[agent.tool_revisions]
servingstudio-sim = "<ServingStudioSim commit>"
```

The run checks the table (a lower-case tool name, a 7 to 64 character hexadecimal commit) and
does nothing else with it, in task schema 0.1 and 0.2 alike. Tasks that need different
revisions need one checkout each.

```bash
PIN=<the task's servingstudio-sim, or a commit from assets/pins.toml>
git clone --recurse-submodules https://github.com/SyFI-ServingStudio/ServingStudioSim.git
cd ServingStudioSim && git checkout "$PIN" && git submodule update --init --recursive --checkout
just sync
uv run cargo build --release --workspace
uv run python -m launcher presets/unified_smoke.yaml --no-gpu   # three requests must finish
```

For Trainium use the `[trainium]` commit: trn2 support is on an unmerged pull request, so
`main` cannot simulate it. Put the checkout outside every repository an agent optimizes.

## Adding a model or hardware

A checkout simulates only the models and hardware it has presets for. Adding one is
ServingStudio development, done here, before the run, with the user's approval; never as a plan
of an optimization that needs it to lead. It takes five things, and the second costs the most:

1. **The model's structure:** the operations of one forward pass and their shapes, split across
   the devices the way the engine will split them. A model close to one already supported is
   mostly configuration; a new architecture on a new device is a new architecture module.
2. **A measured time for every kernel at every shape the workload reaches** (batch sizes,
   context lengths, prefill chunks): microbenchmarks of single operations on the hardware, not a
   serving engine. The preset lists the rows it needs and refuses a workload while one is
   missing; measure them, never guess them.
3. **The engine's scheduling:** batching, chunked prefill, speculative decoding, KV admission.
   Mostly settings of what the simulator already models.
4. **The workload:** the benchmark's trace, converted to the simulator's format.
5. **A check against measured numbers:** reproduce measured step times of some engine on this
   hardware (an earlier port, a public engine), on points held out of any fit. That says the
   simulator can rank designs. Its absolute numbers wait for a calibration
   ([calibration.md](calibration.md)), which needs a measured engine; it need not be the one the
   run will build.

It is ready to lead an optimization when step 1 of the skill finds a match for the task and a
simulation of the task's workload runs to the end with no missing row. Say which of the five is
missing when it is not.

## Hand it to the agents

Export the variables in the environment the agents inherit, as absolute values:

```bash
export SERVINGSTUDIO_API=<endpoint>      # omit to use the hosted service
export SERVINGSTUDIO_SIM=<absolute path>  # only with a local checkout
```

With a local checkout, the lead also creates the task's records directory once and names it as
`SERVINGSTUDIO_RECORDS` ([calibration.md](calibration.md#where-records-live)); it is kept
across runs.

A lead passes these to every subagent it briefs that will use this skill, as it passes its other
tool paths. Where the agents run under a host that clears its environment (the VibeSys
orchestrator keeps only a fixed list of variables), the operator states the values in the
run's brief and the lead passes them on; a variable set in the operator's shell does not arrive.

## Check

```bash
python3 <skill-dir>/scripts/servingstudio.py health
git -C "$SERVINGSTUDIO_SIM" rev-parse HEAD
```

`health` prints the endpoint and the simulator revision it serves (`sim_commit`). Every
prediction names that revision, or the checkout's `HEAD`. A revision that differs from the pin
is allowed; one that changes during an optimization makes earlier and later predictions
incomparable, so do not upgrade a simulator under a running optimization.
