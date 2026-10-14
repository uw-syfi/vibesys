---
name: write-search-flow
description: Add or change a search, the outer loop an operator picks for a run, written as a vs-core flow; covers the search's root flow, roles and corrections, operations, prompts, workspace settlement and admission, the winner, the core policy projection, launch options, wiring, module-graph edges, and the search's stories, fault, role-override and golden tests. Use when adding a built-in search or preset, or changing a search's flows, roles, operations, prompts, budget, wiring or tests.
metadata:
  kind: codebase
---

# Write a search flow

A **search** is a run's outer optimization loop: it picks ideas, has agents
implement them in candidate workspaces, has candidates measured, and names a
winner. Each built-in search is a **flow**: an `async def` that takes
`ctx: Flow` first and touches the world only through `ctx`; `vs-core` journals
what it observes and replays it after a crash, so it must be deterministic.
[flows.md](../../../docs/contributing/flows.md) is the vocabulary. To learn the
shape of a search, read the smallest built-in one end to end: its package, its
role and prompt declarations, its wiring and its tests. Find the built-ins in
the orchestration catalog reached through `vibesys.api`.

## Builds on

- `software-design`: pure core, thin shell. Decisions live in pure helpers
  that a flow only sequences; the flow itself just awaits.
- `testing`. The shared test hosts are the `write-vibesys-tests` skill's.

## When this applies

- Adding a built-in search or a preset (a plugin id that reuses a search with
  other options).
- Changing a search's flows, config, roles, operations, prompts, budget,
  settlement, admission or published record, or its core policy projection.
- Changing its wiring: the plan builder, the host roles, the launch options,
  the catalog entry, the command-line choice, or its module-graph entries.

## Steps

1. **Keep the package shape**: private modules behind an `api` facade with
   `__all__`, and `vs_core` imported only through `vs_core.api.search` (only
   the core policy may read `vs_core.api`).
   `test_a_search_package_imports_vs_core_only_through_the_search_facade`
   fails otherwise.
2. **Write flow code only against `ctx`**: no clock, randomness, threads,
   files, sockets, `asyncio` or blind `except`; a missing capability goes into
   the `vs-core` flow layer, reviewed.
   `test_flow_modules_use_only_ctx_for_the_outside_world` and `BLE001` fail
   otherwise.
3. **Register the root** with the `search` decorator (name, config, operations,
   roles; plus `owned=` for operations a library owns) and call
   `ctx.checkpoint(config)` first. The config is a `Value` with only the
   search's own knobs. Name the winner as you go with `ctx.choose`; return
   `None` or `NoWinner(reason)` and never finish the run.
   `test_a_root_that_returns_keeps_the_input_and_the_run_succeeds` and
   `test_no_winner_fails_the_run_without_asking_core_to_adopt` pin the ending.
4. **Make every child flow restartable**: `@flow(snapshot=S)` calling
   `ctx.checkpoint(...)` first, or a resume on changed code raises
   `FlowCodeChanged`. Unenforced: no search test resumes on changed code.
5. **Declare each agent question once as a `Role`**: template, reply model,
   workspace, access, turn time, `corrections` (fixes of an invalid reply),
   `lost_turns`, charge, and whether it may wait for evaluations. The reply is
   the role's own model; core wraps it for a waiting role. `agent=` puts a
   second question to another role's agent. Only `model` changes per run.
   `test_a_chosen_model_changes_that_role_and_nothing_else` and
   `test_how_a_role_is_carried_is_never_left_to_a_default` fail otherwise.
6. **Give every agent its host role** next to the search's wiring: system
   prompt template, tools, and the evaluation tool for a role that waits.
   `test_a_plugin_declares_one_agent_per_agent_its_search_asks` and
   `test_an_agent_whose_role_may_wait_for_evaluations_carries_the_evaluation_tool`
   fail otherwise.
7. **Name operations in the search's namespace**: one `StrategyOperations`
   per search, kinds `<namespace>.<op>`. Kinds are persisted; never rename one.
   `test_the_kinds_schemas_and_roles_follow_the_namespace` fails otherwise.
8. **Send typed prompt contexts, never prompt text**: a `RolePromptContext`
   naming its template, the template in the core policy's prompt templates,
   run-level variables from the core policy. Render every template over its
   contexts. `test_a_template_names_reply_fields_only_through_reply` fails on
   typed reply field names in a template.
9. **Open workspaces only as resources and settle through shared policy**:
   `async with ctx.workspaces.open(...)`; handle `NotOpened`, a `Declined`
   checkpoint and `Stopped`; `keep` an `Assessment` from the shared candidate
   or serial-search API.
   `test_a_search_ends_cleanly_whatever_core_does_to_its_waits_and_however_it_stops`
   fails if an attempt is left unsettled or the run does not finish once.
10. **Admit every finished task in an order independent of finish order**:
    fold each value a group yields, `TaskStopped` included, and admit
    concurrent results in slot order as evolve's round does.
    `test_admission_is_in_slot_order_exactly_once_whatever_order_children_finish_in`
    fails otherwise.
11. **Derive the budget** from the config and each role's
    `turn_chain(corrections=, lost_turns=)`. Unenforced: only evolve's budget
    has a test.
12. **Publish and project**: `ctx.publish(snapshot)` after each fold; the core
    policy's run-snapshot projection maps it to the `IterationTree` clients
    navigate; declare `IterationSupport.PROJECTED`.
    `test_catalog_has_an_explicit_iteration_disposition_for_every_builtin`
    fails otherwise.
13. **Keep launch options and reply models strict**: an undeclared key is
    refused. `test_every_model_forbids_unknown_keys_or_declares_why_not` fails
    otherwise.
14. **Wire and register**: the plan builder (with `search.with_models(...)`
    for `agent.toml` overrides) and the projector; the plugin exports its
    registration; add it to the built-in catalog in `vibesys.api`; map the
    outer-loop command-line choice to it.
    `test_every_direct_orchestration_folder_registers_a_strategy` fails
    otherwise.
15. **Add module-graph entries and edges** for the search, its core policy,
    launch, host roles and plan builder, each search exposing only `api`.
    `check_tach` fails on a missing or upward edge.

## Testing

- Tell each decision as a story: a per-search host drives `FlowRun` from
  `vs_core.api.testing`, and the shared flow test helpers rerun it with
  restarts. Unenforced: the stories are the author's choice of scenarios.
- List a new plugin in the fault-play and role-declaration test tables;
  `test_every_built_in_search_is_played_under_faults` fails until it is.
- Property-test pure helpers. Unenforced: judgment about which helpers merit it.
- Add a role-override test: `agent.toml` keys address the plugin's role ids,
  and any other key, legacy ids included, is refused naming it.
  `test_a_legacy_role_id_is_refused_naming_the_key` is the model to copy;
  Unenforced: nothing requires one per plugin.
- Pin agent-facing text with system-prompt tests and exact-text goldens.
  `test_the_round_prompt_renders_for_every_context` shows the render-all
  pattern. Unenforced: goldens are opt-in per prompt.
- Add the search's world to the crash-walk test's world table.
  Unenforced: that table is keyed by world, not plugin id.

## Known failure modes

- A slot that finished other than by a clean settlement was never admitted, and
  the run stalled:
  `test_a_failed_child_is_admitted_like_any_other`.
- A write turn whose checkpoint core declined was treated as a reply; match
  `Declined`. The fault menu declines at random:
  `test_a_search_ends_cleanly_whatever_core_does_to_its_waits_and_however_it_stops`.
- A strategy's prompts lacked the domain pack:
  `test_every_role_prompt_carries_its_domains_section_and_no_other_domains_text`.
- A refactor that changes a decision's id or payload breaks journal
  deduplication on restart; the dynamic search pins its decisions in
  `test_decisions_are_identical_to_the_recorded_transcript`.
  Unenforced: the other searches have no decision transcript.

## Sources of truth

- [flows.md](../../../docs/contributing/flows.md): the flow vocabulary (its
  `role.retry.corrections` is `Role`'s `corrections`).
- `vs_core.api.search` and `vs_core.api.testing`: what a search and its tests
  may import.
- The `Role` declaration in `vs-core`: fields, defaults and caps.
- The outer-loop command-line choices, the built-in orchestration catalog in
  `vibesys.api`, and the module graph that `check_tach` enforces.
