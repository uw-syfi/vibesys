---
name: add-compute-backend
description: Add a compute backend (an accelerator platform such as a GPU vendor, a Trainium-style accelerator or a plain CPU) as a set of coordinated edits, with completeness tests that name any piece left out. Use when adding or changing a compute backend member or its implementation in vs-sandbox, backend prompt fragments, or per-platform guidance in bundled skills.
metadata:
  kind: codebase
---

# Add a compute backend

A compute backend is the accelerator platform a run targets (`--backend`). It
decides how the agent's command runner is built (image, device forwarding,
environment), what agents are told about devices and data types, which
profiler `auto` picks, and which platform guidance bundled skills show. The
`ComputeBackend` enum is owned by `vs-sandbox` and reached through
`vs_sandbox.api`; the product re-exports it, and CLI choices, skill-metadata
validation and platform pruning derive from it.

## Builds on

- `software-design`: rule 14. A backend is a `<Variant>Backend` implementing
  the compute-backend interface, and passes that interface's contract suite.
- `testing`: rule 3 (one contract suite for Fakes and real implementations).

## When this applies

- A new `ComputeBackend` member, or a change to how one builds its runner.
- Backend prompt fragments, and the per-platform guidance in bundled skills.

## Steps

1. **Add the member and register a factory; let the tests list what is
   missing.** `test_every_compute_backend_has_a_registered_factory` fails,
   naming the backend, until it has a factory;
   `test_every_backend_has_a_default_profiler_that_accepts_it` and
   `test_platform_guidance_covers_exactly_the_known_backends` fail until the
   product tables in steps 5 to 7 cover it.
2. **Choose the implementation.** A platform with device logic gets its own
   `<Name>Backend`, exported from `vs_sandbox.api`, reading devices through an
   injected accelerator-discovery interface. A platform with no device to
   manage binds the shared local backend. Add the class to the contract
   suite's factories. Unenforced: real constructors probe hardware, so no test
   builds every registered backend.
3. **Declare a host-only platform** (no container reaches its accelerator) as
   host-only on the backend; run-environment selection asks that capability,
   never the name. `test_only_metal_declares_itself_host_only` pins the set, so
   update it when the set changes.
4. **Pick the default Docker image**; see `change-container-images`.
   Unenforced: only running the image shows it carries the platform's
   toolchain.
5. **Write the prompt fragments**: a fragment directory for the backend with
   every fragment name, and a fragment class registered for it (prose via
   `write-agent-prompt`; an empty file is a deliberate skip).
   `test_every_backend_has_registered_fragment` and
   `test_every_backend_fragment_validates` fail for a backend missing either.
6. **Pick the profiler `auto` uses** for the backend and allow the backend on
   that profiler's definition (`add-profiler`);
   `test_every_backend_has_a_default_profiler_that_accepts_it` checks the pair.
   Extend the backend map in `test_profiler_auto_resolution_exhaustive`.
7. **Add platform guidance** for the backend in every bundled skill that has a
   platforms directory. An agent sees only its own backend's;
   `test_platform_guidance_covers_exactly_the_known_backends` fails for a
   missing directory or one named after no backend, which pruning would never
   hide.

## Testing

- Add the backend to the contract suite (`test_satisfies_the_protocol` and its
  siblings run every implementation); also run the wiring, fragment and
  profiler tests named above.
- Test discovery with the Fake accelerator discovery, which
  `test_system_and_fake_implement_the_same_inventory_contract` holds to the
  real one, and forwarding as a property over device sets
  (`test_groups_without_devices_are_refused`); a real device run belongs in a
  real tier.

## Known failure modes

- Device groups added to a container with no devices forwarded;
  `test_groups_without_devices_are_refused`.
- An `auto` profiler that did not fit the backend;
  `test_every_backend_has_a_default_profiler_that_accepts_it`.
- A branch on a backend's name outside the wiring code; declare a capability
  instead, as host-only does. Unenforced: needs judgment.

## Sources of truth

- The `ComputeBackend` enum, interface and registry in `vs-sandbox`, reached
  through `vs_sandbox.api`.
- The compute-backend prompt fragment registry, which fixes the fragment names.
- The platform-pruning policy that hides other backends' platform guidance.
- [sandboxing.md](../../../docs/contributing/sandboxing.md): run environment and
  command runner vocabulary.
