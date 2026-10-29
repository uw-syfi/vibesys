import type {HelperFeature} from '@vibesys/contracts/host-helper';

/**
 * What a host's VibeSys offers over the gateway: every feature the screens can ask about. A host
 * that predates the registry answers each call with the typed "unsupported" verdict, so the screens
 * need no negotiation with a helper; the record makes a new feature name fail to compile until it
 * is listed here.
 */
const NATIVE: Readonly<Record<HelperFeature, true>> = {
  instances_watch: true,
  paths_complete: true,
  instances_inspect: true,
  logs: true,
  logs_cancel: true,
  harnesses: true,
  logs_by_span: true,
  run_history: true,
  run_summary: true,
  run_history_pages: true,
  run_span_events: true,
  run_tail: true,
  run_trace_events: true,
  machine_describe: true,
  checkout_inspect: true,
  preflight: true,
  run_options: true,
  examples: true,
  run_roles: true,
  project_clean: true,
  run_backend: true,
  run_causes: true,
  example_catalogue: true,
  run_digest: true,
  run_dashboard: true,
  harness_generations: true,
  run_role_agents: true,
  measurement_target: true,
  machine_compute: true,
  agent_logins: true,
  project_goal: true,
  example_destination: true,
  usage_window: true,
  run_trace: true,
  run_ending: true,
  chat_ended_runs: true,
  chat_ended_run_questions: true,
};

export const NATIVE_FEATURES: readonly HelperFeature[] = Object.keys(NATIVE) as HelperFeature[];
