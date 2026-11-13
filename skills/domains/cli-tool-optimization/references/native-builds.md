# Native builds

A program is measured as a built executable, so the build is part of the
experiment. This reference covers rebuilding correctly and the decisions that
change the executable independently of your source edit.

## Always build through the task's commands

Use the task-provided build command for anything you intend to test, measure or
submit. A private build with other flags, a different compiler, or a different
configuration is a different program. If a private build helps you profile
(extra debug info, instrumentation), keep it separate and never time it against
the real one.

## Build systems you will meet

- **Autotools** (`configure`, `make`): configuration choices land in generated
  headers and Makefiles. Re-running `configure` can change the build in ways
  unrelated to your edit. Generated sources (`Makefile.in`, parsers, lookup
  tables) may not rebuild when their inputs change; know which are checked in and
  which are generated.
- **Plain make and CMake**: check that header dependencies are tracked, or an
  edited header will not rebuild its users.
- **Cargo**: profiles (`opt-level`, `lto`, `codegen-units`, `panic`, debug
  assertions) and feature flags change the executable. A change to the manifest
  is a build change even if no source file moved.
- **Distribution-style builds** apply the packager's flags (often `-O2`, debug
  info, hardening, position-independent code). If the task measures a
  distribution-style build, optimize that build, not an idealized one. The
  packaging rules, patch series and configure options may be inside the permitted
  scope; changing them is a build change, with everything that implies here.

## Clean versus incremental

Incremental builds are fast and sometimes wrong: stale objects, missing header
dependencies, unrebuilt generated sources and clock skew can leave an executable
that does not contain your change. Before trusting a result, rebuild from clean
at least once, confirm the correctness check passes on that build, and confirm
the measured executable is the one you built (version string, checksum, or build
id). Do not let a compiler cache hide a stale result. Prefer builds that are
reproducible: the same inputs give the same executable, with no embedded
timestamps or absolute paths where the build allows it.

## Compiler and linker flags

Flags are legitimate optimizations when the task allows build changes, and they
change behavior in ways source review will not show. Treat each as its own
experiment:

- **Optimization level and inlining** can change speed and, where the program
  has undefined behavior, its output. Re-run correctness after any change.
- **Semantics-changing flags** (fast-math style float relaxations, removing
  strict-aliasing or overflow protections) can alter results. Do not use them
  unless the objective allows it.
- **Link-time optimization (LTO)** can help across translation units. It lengthens
  builds, and its benefit depends on the toolchain version.
- **Profile-guided optimization (PGO)** trains the compiler on a workload. A
  profile gathered from the scored invocations bakes the benchmark into the
  binary. Use PGO only if the task permits it, regenerate the profile through the
  task's build, and train on a broad, representative set, not the measured cases.
- **Linker choices** change start-up. Lazy versus immediate binding, symbol
  visibility and static versus dynamic linking all affect dynamic-loading cost.
  Static linking can also change behavior of name lookup, locale and
  internationalization, and it affects licensing and size.
- **Hardening** (stack protectors, fortification, relocation read-only,
  position-independent code) costs a little speed. Removing it is a security and
  portability decision, not a free win; do not drop it without authority.

## Portable versus machine-specific optimization

Code or flags that assume the build machine's CPU (`-march=native` or an
equivalent such as a fixed vector-instruction level) produce an executable that
may fail or misbehave elsewhere. Do not use them as a default. If a task allows
them, **state the assumption plainly** in your record and in the change
description, so a reviewer can accept or reject it knowingly. Prefer portable
alternatives: runtime CPU detection with a safe fallback, or algorithmic
changes that need no special instructions.

## New dependencies

A new library changes the build, the portability and often the license
obligations. Before adding one, ask whether an in-tree change gives most of the
gain, whether the task's scope permits it, and whether the build environment has
it. A new shared library may also be missing from the environment that runs the
program, where the program then fails to start; prefer none. Disclose any dependency
you add, with the reason and the measured benefit.
Vendoring third-party code carries the same questions plus its license.

## Build changes can mislead the experiment

A build-system change can move the measured time without any change to the
source-level hypothesis you are testing, and a source change can interact with a
build setting (inlining, layout) so that its effect is not what you intended.
Change one of them at a time, rebuild clean, and rerun correctness and
measurement after each.
