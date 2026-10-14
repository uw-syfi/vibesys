# ComputeBackend fragments

This directory holds **fragments** — small reusable Jinja snippets that get composed into parent prompts based on the run's selected backend.

```
backend/
├── cuda/                            ← cuda fragments
│   ├── device_dtype.j2
│   ├── judge_device_correctness.j2
│   └── profiling_workflow.j2
├── metal/                           ← metal fragments (mirrors cuda)
├── rocm/
├── trainium/
└── cpu/
```

Every backend directory carries the same three fragments.

## How they're used

`BackendPromptRenderer(template_dir, backend)` (in `vibesys/prompts/renderer.py`) auto-injects every fragment under `backend/<backend>/` as a kwarg keyed by **filename stem** on every `prompt.render(...)` call.

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
   fragments — let `BackendPromptRenderer` handle the resolution.

## Adding a new backend

Follow the `add-compute-backend` agent skill (`.agents/skills/add-compute-backend/`).
For this directory: create `backend/<new>/` with every name in
`ComputeBackendFragment.NAMES`, and add a `ComputeBackendFragment` subclass for
the backend in `vibesys/prompts/renderer.py`, registered in `_FRAGMENT_IMPLS`.
`tests/vibesys/test_backend_fragments.py` fails until both exist.

## Python contract

The canonical list of fragment names lives on `ComputeBackendFragment.NAMES` (in `vibesys/prompts/renderer.py`), not in this directory layout. Adding a fragment is a 3-step change:

1. Add the name to `ComputeBackendFragment.NAMES`.
2. Create `<name>.j2` under every `backend/<backend>/` directory
   (empty file = hard skip; placeholder prose = soft skip).
3. Reference `{{ <name> }}` from the parent template that needs it.

`BackendPromptRenderer` calls `ComputeBackendFragment.validate()` at construction, which checks that every name in `NAMES` has a corresponding `.j2` file under the bound backend's directory. A missing file fails fast with a clear error naming the path — silent empty-string rendering is no longer a failure mode.

### Skip flavors

- **Hard skip** — empty `.j2` file. The fragment kwarg renders to an
  empty string. Use when the fragment topic genuinely doesn't apply
  to this backend and silence is better than confusion.
- **Soft skip** — short placeholder prose explaining the absence
  (e.g. metal's `profiling_workflow.j2` says "Profiler-guided
  analysis is not yet wired up..."). Use when the LLM benefits from
  knowing the topic exists but the implementation doesn't.

Both pass validation; `validate()` only checks the file exists.
