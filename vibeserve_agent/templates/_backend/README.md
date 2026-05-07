# ComputeBackend fragments

This directory holds **fragments** — small reusable Jinja snippets that get composed into parent prompts based on the run's selected backend.

```
_backend/
├── cuda/                            ← cuda fragments
│   ├── device_dtype.j2
│   ├── judge_device_correctness.j2
│   └── profiling_workflow.j2
└── metal/                           ← metal fragments (mirrors cuda)
    ├── device_dtype.j2
    ├── judge_device_correctness.j2
    └── profiling_workflow.j2
```

## How they're used

`Prompt(template_dir, backend)` (in `vibeserve_agent/prompts.py`) auto-injects every fragment under `_backend/<backend>/` as a kwarg keyed by **filename stem** on every `prompt.render(...)` call.

So a fragment file named `device_dtype.j2` is auto-injected as the kwarg `device_dtype`, and any parent template can reference it as `{{ device_dtype }}`. The parent template doesn't know or care which backend it's rendering against.

Explicit kwargs passed to `prompt.render(...)` override auto-injected fragments — useful when a caller wants to substitute custom prose for a single fragment without changing the fragment file.

## Conventions for adding a new fragment

1. Pick a stem that reads cleanly as a Jinja variable
   (e.g. `gpu_env_setup`, not `setup-gpu`).
2. Create the fragment in **every** backend directory
   (`cuda/<stem>.j2`, `metal/<stem>.j2`, …). Missing files mean a
   template that uses `{{ stem }}` will silently render empty under
   that backend.
3. The fragment should be a self-contained snippet — typically one to
   a few sentences, or a single fenced code block. No `{% block %}`,
   no full document headers; the parent template owns structure.
4. Reference it from a parent template with a simple
   `{{ stem }}`. Don't use `{% include %}` for backend-specific
   fragments — let `Prompt` handle the resolution.

## Conventions for adding a new backend

1. Add the variant to `ComputeBackend` in
   `vibeserve_agent/constants.py`.
2. Create `vibeserve_agent/templates/_backend/<new>/` and mirror every
   name in `ComputeBackendFragment.NAMES` (currently: `device_dtype.j2`,
   `judge_device_correctness.j2`, `profiling_workflow.j2`). Use an
   empty file for a deliberate skip, or short placeholder prose for
   a soft skip — don't leave the file out, validation will fail.
3. Add a concrete `ComputeBackendFragment` subclass in
   `vibeserve_agent/prompts.py`:
   ```python
   class RocmComputeBackendFragment(ComputeBackendFragment):
       backend = ComputeBackend.ROCM
   ```
   …and register it in `_FRAGMENT_IMPLS`.
4. Wire up the backend's runtime impl under
   `vibeserve_agent/backends/<new>/` and register it in
   `backends/__init__.py`. See the existing CUDA and Metal impls for
   the pattern.

## Python contract

The canonical list of fragment names lives on `ComputeBackendFragment.NAMES` (in `vibeserve_agent/prompts.py`), not in this directory layout. Adding a fragment is a 3-step change:

1. Add the name to `ComputeBackendFragment.NAMES`.
2. Create `<name>.j2` under every `_backend/<backend>/` directory
   (empty file = hard skip; placeholder prose = soft skip).
3. Reference `{{ <name> }}` from the parent template that needs it.

`Prompt.__init__` calls `ComputeBackendFragment.validate()` which checks that every name in `NAMES` has a corresponding `.j2` file under the bound backend's directory. A missing file fails fast with a clear error naming the path — silent empty-string rendering is no longer a failure mode.

### Skip flavors

- **Hard skip** — empty `.j2` file. The fragment kwarg renders to an
  empty string. Use when the fragment topic genuinely doesn't apply
  to this backend and silence is better than confusion.
- **Soft skip** — short placeholder prose explaining the absence
  (e.g. metal's `profiling_workflow.j2` says "Profiler-guided
  analysis is not yet wired up..."). Use when the LLM benefits from
  knowing the topic exists but the implementation doesn't.

Both pass validation; `validate()` only checks the file exists.
