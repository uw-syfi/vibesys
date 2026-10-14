---
name: add-profiler
description: Add a profiler the profiler agent can call (a GPU kernel profiler, a native CPU profiler, a headroom estimator) as a set of coordinated edits, with a completeness test that names any piece left out. Use when adding or changing a profiler kind or its definition, a bundled profiler MCP server, a profiler prompt, or the profiler auto-selection policy.
metadata:
  kind: codebase
---

# Add a profiler

A profiler gives the profiler agent evidence-gathering tools through an MCP
server (a tool server the agent's CLI calls). A run picks one with
`--profiler`, or `auto` picks from the domain, the compute backend and what the
run environment can execute. A profiler is four pieces: a member of the
profiler kind enum; a `ProfilerDefinition` in the profiler registry (domains,
backends, the executable it needs and where it runs, tools that need a GPU
driver); a bundled MCP server staged into the agent's workspace; and a prompt
template. Directory, server and prompt names all derive from the kind
([extending-profilers.md](../../../docs/contributing/extending-profilers.md)).

## Builds on

- `software-design`: rule 10 (distrust external boundaries). An agent's tool
  call is untrusted input; failures come back as structured results.
- `testing`: rule 7. Test a tool server with synthesized calls, not agents.

## When this applies

- A new profiler kind, a changed profiler definition, or `auto` selection.
- A bundled profiler server or a profiler prompt template.

## Steps

1. **Add the member and its definition; let the test list what is missing.**
   `test_every_profiler_kind_has_a_definition`,
   `test_every_definition_ships_a_server_and_a_prompt` and
   `test_every_support_package_and_prompt_belongs_to_a_definition` fail,
   naming the kind, until every member except `auto` and `none` has a
   definition, a server and a prompt, and every server and prompt has a
   definition.
2. **Declare compatibility on the definition**, and keep `auto` choices in the
   profiler resolution policy, never in callers.
   `test_profiler_auto_resolution_exhaustive` covers every domain, backend and
   requested kind.
3. **Declare the executable and its site** (the agent's shell or the GPU
   workload) so run start refuses a profiler the environment cannot run.
   `test_a_profiler_is_runnable_where_it_resolves_or_refused_with_a_named_reason`
   checks every combination. The agent image must carry the executable; see
   `change-container-images`.
4. **Build the server on the strict server base** the bundled profilers share,
   so every tool refuses an argument it does not declare;
   `test_a_tool_refuses_an_argument_it_does_not_declare` checks every server.
5. **Withhold tools whose hardware is missing.** List tools that need a GPU
   driver on the definition and make the server drop them when the run
   withholds them. `test_driver_tools_are_real_tools_the_server_can_withhold`
   checks the server side; `test_offer_withholds_exactly_the_driver_tools_without_an_editor_gpu`
   checks when a run withholds them.
6. **Put a native CPU profiler's mechanism in `vs-runtime`**, behind a
   stdlib-only module the server imports on the agent's interpreter, and wire
   its host check into the native-profiler preflight. Unenforced: the
   preflight runs real host programs, so no deterministic test covers it.
7. **Write the prompt** with `write-agent-prompt`, using the variables in
   [extending-profilers.md](../../../docs/contributing/extending-profilers.md);
   `test_every_packaged_prompt_renders_without_checkout_fallback` renders every
   packaged prompt.

## Testing

- Call tools in process through the server's builder, as
  `test_ncu_server_reports_missing_executable` does; server tests live with the
  product tests, never beside the shipped server. Unenforced: placement of a
  test file needs judgment.
- A missing executable, a malformed report or a failed analyzer is a tool
  error, never a normal reply: `test_every_nonzero_ncu_counter_set_exit_is_a_tool_error`
  and its siblings.
- Read workload-written JSON through the shared bounded reader, and add each
  new decoder to the hostile-JSON corpus that
  `test_a_decoder_answers_every_hostile_frame_with_its_value_or_its_own_rejection`
  runs. Unenforced: that corpus lists decoders by hand.

## Known failure modes

- Offered where it cannot run: an `auto` profiler that did not fit the backend,
  or driver tools offered in a GPU-less editor;
  `test_auto_picks_a_profiler_that_fits_the_backend_or_says_why_it_picked_none`.
- A misspelled optional argument silently replaced by its default, because
  FastMCP drops unknown keys; `test_a_tool_refuses_an_argument_it_does_not_declare`.
- Profiler timings presented as the scored result; the prompt must keep the
  trusted benchmark separate. Unenforced: needs a reader of the prompt.

## Sources of truth

- The profiler registry and the `auto` resolution policy in `vibesys`, and the
  run-environment binding that decides what a run can execute.
- [extending-profilers.md](../../../docs/contributing/extending-profilers.md).
