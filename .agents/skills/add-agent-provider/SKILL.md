---
name: add-agent-provider
description: Add or change support for an agent CLI provider, upgrade the agentshim library that drives those CLIs, or bump a provider CLI version pinned in the agent image. Use when shipping or dropping a provider, changing the agent Dev Container Feature that installs provider CLIs, moving the agentshim pin, or changing provider selection (the --cli-provider flag, the agent cli_provider setting, quota fallback providers).
metadata:
  kind: codebase
---

# Add an agent provider

A **provider** is an agent CLI VibeSys can drive. The shipped set lives in the
provider registry in vs-agent, reached through `vs_agent.api`; read it there
rather than from this skill. A provider is not the **agent backend**
(`cli` or `stub`): the backend chooses between driving a provider CLI and a
canned stub runner, and providers exist only under `cli`.

VibeSys never talks to a CLI directly. **agentshim**, a separate library pinned
in `pyproject.toml`, owns provider knowledge (argv, stream parsing, MCP config
formats, schema dialects, state directories, auth variables, skill
directories, resume flags, each CLI's models and their context windows),
published per provider as a `ProviderProfile`. VibeSys owns **driver
policy**: which providers it ships, the CLI versions in its container image,
budgets, confinement and quota handling. See
"Where agentshim lives" in
[agent-drivers.md](../../../docs/contributing/agent-drivers.md), which also
shows the temporary override for an unreleased agentshim commit.

## Builds on

- `software-design`: one source of truth per fact; extend the existing seams
  (the provider policy for decisions, the profile accessor for facts).
- `testing`: Fakes over mocks; tests use `vs_agent.api.testing`, never a CLI.

## When this applies

- Shipping or dropping a provider; upgrading agentshim; bumping a CLI or Node
  pin for the agent container.
- Changing provider selection: the `--cli-provider` flag, the agent
  `cli_provider` setting, or the agent quota settings.
- Not session, transport or turn-recovery mechanics (agent-drivers.md), nor
  test conventions in general (`testing`, `write-vibesys-tests`).

## Steps

1. **Put CLI behavior in agentshim first.** New argv, parsing, failure
   classification or schema dialects land and release upstream, then the pin
   rises. Remove any temporary source override before merging.
   Unenforced: whether a fact is provider knowledge or VibeSys policy needs
   judgment; ask "is this true because of how the CLI behaves?".
2. **Import agentshim only inside vs-agent.** Everything else uses
   `vs_agent.api`, and tests use `vs_agent.api.testing`. Ruff's banned-api rule
   `TID251` enforces it; export from `vs_agent.api` instead of an exemption.
3. **Read provider facts through vs-agent's profile accessor at call time.**
   Host grants, auth, skill and MCP config locations derive from the profile,
   so they need no per-provider edit. Keep provider-name literals inside the
   provider policy; do not grow the allowlist of the scan.
   `test_no_bare_provider_literals_outside_the_allowlist` fails if a shipped
   provider name appears elsewhere.
4. **Ship it in the provider policy:** add the provider to the shipped set and
   pin its CLI version beside it (the container environment derives from
   them). That set drives agent spec validation (which rejects any other name),
   the `--cli-provider` choices, quota fallback validation, and the Docker
   check when building an agent client.
   `test_shipped_providers_are_registered_with_agentshim` and
   `test_every_shipped_provider_has_a_container_cli_pin_and_no_other_does`
   hold it.
5. **Install the CLI in the agent image**, the Docker layer built over the
   task image from the `vibesys-agent` Dev Container Feature (see
   [devcontainer-features.md](../../../docs/contributing/devcontainer-features.md)).
   It installs every CLI with its own `npm` line and ignores agentshim's
   `ProviderProfile.container_install`, whose pins lag. Add a
   `<provider>_version` option (default = the pin) to the Feature manifest, a
   `require_option` line and the install to its install script, and a
   `<binary> --version` check to its smoke test.
   `test_option_defaults_mirror_provider_policy` and
   `test_the_feature_requires_and_smoke_tests_every_shipped_cli` tie them to
   the policy; only the `devcontainer features test` CI job proves the install
   works.
6. **Upgrade agentshim:** raise the range, relock, read its changelog for
   renamed profile fields and new CLI minimums, and run the vs-agent tests.
   `test_the_container_codex_cli_is_not_older_than_agentshims_protocol` fails
   when its generated Codex protocol needs a newer CLI than the pinned one.
   A relock also changes the models VibeSys admits and the context windows it
   shows (agentshim's facts; `test_no_model_knowledge_in_product_code`): diff
   each `ProviderProfile.models` and the context table across versions, and list
   added and removed models in the PR. Unenforced: no check compares releases.
7. **Bump a CLI pin** in the provider policy and the Feature option default
   together (Codex has its own pin constant, which the pin test above reads).
   `test_option_defaults_mirror_provider_policy` fails if they diverge.
8. **Update hand-written provider lists:** the `cli_provider` setting's
   description, the example agent config, chat model suggestions, and the
   provider tables in agent-drivers.md. Unenforced: prose and suggestion data;
   a check would have to parse English.

## Testing

- Script provider turns with `vs_agent.api.testing.FakeProvider` through the
  real `AgentClient`. When the subject is a derivation from a
  `ProviderProfile`, build profiles with `vs_agent.api.testing.fake_profiles`
  so a library release cannot break the test.
  Unenforced: choosing the right Fake is judgment; the `testing` skill covers it.
- These pick up a new provider automatically:
  `test_default_driver_supports_all_agentshim_providers` and
  `test_a_provider_receives_the_fields_the_reply_offers`, which checks every
  reply field survives the provider's schema dialect.
- Readiness (missing binary, failed login):
  `test_a_missing_binary_fails_before_any_turn` and
  `test_a_failed_login_fails_before_any_turn_with_the_providers_fix`. Docker
  auth presence:
  `test_docker_environment_rejects_a_cli_provider_without_any_auth_source`.
  Quota config: `test_a_bad_quota_section_is_rejected_naming_the_offending_key`
  and `test_a_fallback_with_no_wait_budget_switches_at_once`.
- Real CLIs run only in the e2e tier, skipped unless `VIBESYS_E2E_AGENTS=1`
  (and `VIBESYS_E2E_DOCKER=1` for container tests); the confined-session e2e
  covers Claude and Codex only, and the image e2e builds the image and runs every shipped CLI. Run them
  after a pin bump or upgrade and paste the output in the PR.
  `test_cpu_agent_image_runs_every_shipped_cli_as_the_agent_user` is the image
  check.

## Known failure modes

- A container CLI older than agentshim's generated protocol cannot decode its
  own server, which broke stream turns for Codex and was found only by a live
  run; agentshim's own install recipe still names the old version.
  `test_the_container_codex_cli_is_not_older_than_agentshims_protocol` catches it.
- An agentshim upgrade changed schema normalization and dropped field
  descriptions from reply schemas.
  `test_planner_native_schema_preserves_field_annotations` checks the schema a
  provider receives; it covers Claude and Codex only, so extend it.
- Provider literals copied into a launcher, entrypoint or container table
  drifted apart; the provider policy ended that.
  `test_no_bare_provider_literals_outside_the_allowlist` keeps it ended.
- A quota `fallback_provider` parses, but a run binds every role to one
  provider, so the run host refuses it at startup instead of ignoring it; the
  switch serves only hosts whose sessions can change provider.
  `test_a_fallback_a_core_run_cannot_switch_to_fails_at_startup_naming_the_key`
  holds it.
- Unit tests pass while the real CLI fails: no deterministic test starts a
  CLI. Unenforced: the real tiers need credentials and network, so CI cannot
  run them on every PR.

## Sources of truth

- The provider policy in vs-agent: shipped providers, default, container
  environment, Node and CLI pins.
- The profile accessor in vs-agent: the one seam reading agentshim's
  `ProviderProfile`.
- The agentshim range in `pyproject.toml`, and Ruff's `TID251` ban on
  importing agentshim outside vs-agent.
- [agent-drivers.md](../../../docs/contributing/agent-drivers.md) and
  [devcontainer-features.md](../../../docs/contributing/devcontainer-features.md).
