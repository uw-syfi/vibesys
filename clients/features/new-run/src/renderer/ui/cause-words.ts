/**
 * A failed check in words for the New run screen, from its typed cause when it has one: each
 * names the control to change here (the task, the machine, Rounds, Extra arguments) or the next
 * step (Check again), and never a command-line flag. What the run printed about it is kept apart
 * as `raw`, for a collapsible detail, never a headline. A failed setting whose cause is not known
 * (a helper that predates the cause, or a failure with none) gets a generic headline and its
 * message as `raw`.
 */
import {type Backend, type Check, type RunOption, roleLabel} from '../../state/index.js';

/** The words of one failed check. */
export interface FailureWords {
  /** One or two plain sentences: what is wrong and what to change. */
  readonly headline: string;
  /** What the run printed about it, when that adds anything; shown only on request. */
  readonly raw: string | null;
  /**
   * Whether the cause is fixed outside this screen (a file in the project, the machine), so the
   * person checks again afterwards. A cause fixed by changing a control here is re-checked as
   * the control changes.
   */
  readonly recheck: boolean;
}

/** How many changed paths, vetted models or tasks a headline names before it stops. */
const SHOWN = 5;

/** The compute backend labels, so a failure names the hardware as a person says it. */
const BACKEND_LABELS: Readonly<Record<Backend, string>> = {
  cpu: 'CPU',
  cuda: 'NVIDIA GPU (CUDA)',
  rocm: 'AMD GPU (ROCm)',
  metal: 'Apple GPU (Metal)',
  trainium: 'AWS Trainium',
};

/** A compute backend as a person reads it. */
export function backendLabel(backend: Backend): string {
  return BACKEND_LABELS[backend];
}

/** What each typed option is called on this screen, and where it is set. */
const OPTION_CONTROLS: Readonly<
  Record<RunOption, {readonly name: string; readonly reset: string}>
> = {
  rounds: {name: 'a number of rounds', reset: 'Clear Rounds'},
  time_limit: {name: 'a time limit', reset: 'Clear Time limit'},
  results: {name: 'a choice of where results go', reset: 'Set Results to keep them on the machine'},
  role_model: {name: 'a model for each role', reset: "Set each role's model to the run's in Agent"},
  role_reasoning_effort: {
    name: 'a reasoning effort for each role',
    reset: "Set each role's reasoning effort to the default in Agent",
  },
  role_agent: {
    name: 'an agent for each role',
    reset: "Set each role's agent to the run's in Agent",
  },
};

function listed(items: readonly string[]): string {
  const shown = items.slice(0, SHOWN).join(', ');
  const more = items.length - SHOWN;
  return more > 0 ? `${shown}, and ${more} more` : shown;
}

function quoted(items: readonly string[]): string {
  return items.map(item => `"${item}"`).join(', ');
}

function words(headline: string, raw: string | null, recheck: boolean): FailureWords {
  return {headline, raw, recheck};
}

/**
 * The generic words for a failed setting whose cause is not known: it names the inputs a person
 * can change, and keeps the run's own message out of the headline.
 */
function genericWords(check: Check): FailureWords {
  return words(
    "These settings can't be used to start a run. Check the project, task, agent and Extra arguments, then check again.",
    check.detail,
    true,
  );
}

type Cause = NonNullable<Check['cause']>;

function taskWords(cause: Extract<Cause, {kind: `task_${string}`}>, machine: string): FailureWords {
  switch (cause.kind) {
    case 'task_not_found':
      return words(
        `The task "${cause.task}" isn't in this project, which has ${listed(cause.available) || 'none'}. Choose a task, then check again.`,
        null,
        false,
      );
    case 'task_unusable':
      return words(
        `${cause.task === null ? "The project's task" : `The task "${cause.task}"`} can't be loaded. Fix its files in the project, or choose another task.`,
        cause.problem,
        true,
      );
    case 'task_needs_provisioning':
      return words(
        `The task "${cause.task}" builds its project from sources that are copied in when a run starts, which this app does not do yet. Choose another task.`,
        null,
        false,
      );
    case 'task_image_unsupported':
      return words(
        `${cause.task === null ? "The project's task" : `The task "${cause.task}"`} ships a Dockerfile that a run on ${machine} cannot use. Choose another task or another machine.`,
        cause.problem,
        false,
      );
  }
}

function agentWords(
  cause: Extract<Cause, {kind: 'roles_not_declared' | 'model_not_vetted'}>,
): FailureWords {
  if (cause.kind === 'roles_not_declared') {
    return words(
      `The run has no agent role named ${quoted(cause.roles)}. Reset the roles in Agent, or remove them from the project's agent configuration, then check again.`,
      null,
      true,
    );
  }
  const chosen =
    cause.model === null ? 'No model is named' : `The model "${cause.model}" is not vetted`;
  const vetted = cause.vetted.length === 0 ? 'none' : listed(cause.vetted);
  const where = cause.fallback
    ? "Change the fallback model in the project's agent configuration"
    : 'Choose a vetted model in Agent';
  const whose = cause.role === null ? '' : ` for the ${roleLabel(cause.role)} role`;
  return words(
    `${chosen}${whose} for the harness ${cause.provider}. ${where}; vetted models: ${vetted}.`,
    null,
    cause.fallback,
  );
}

function measurementWords(
  cause: Extract<Cause, {kind: 'measurement_target_not_configured' | 'measurement_target_misfit'}>,
  machine: string,
): FailureWords {
  if (cause.kind === 'measurement_target_misfit') {
    return words(
      `The task needs ${backendLabel(cause.backend)}, which the place chosen to measure does not offer. Choose another place to measure in Where it runs, or another task.`,
      null,
      false,
    );
  }
  const place = cause.target.kind === 'slurm' ? `The cluster "${cause.target.cluster}"` : 'Modal';
  const missing = cause.target.kind === 'slurm' ? 'is not set up' : 'has no token';
  return words(
    `${place} ${missing} on ${machine}. Set it up in that machine's Settings, or choose another place to measure in Where it runs, then check again.`,
    null,
    true,
  );
}

/** The words for `check`'s typed cause; `null` for a check that has none. */
export function failureWords(check: Check, machine: string): FailureWords | null {
  const cause = check.cause ?? null;
  if (cause === null) return check.id === 'settings_valid' ? genericWords(check) : null;
  switch (cause.kind) {
    case 'project_dirty':
      // The host's detail and fix are the words. The app does not add its own.
      return null;
    case 'backend_unavailable': {
      const wants =
        cause.declared.length > 1
          ? `one of ${cause.declared.map(backendLabel).join(', ')}`
          : backendLabel(cause.backend);
      return words(
        `The task "${cause.task}" runs on ${wants}, which ${machine} does not have. Choose another task or another machine.`,
        null,
        false,
      );
    }
    case 'task_not_found':
    case 'task_unusable':
    case 'task_needs_provisioning':
    case 'task_image_unsupported':
      return taskWords(cause, machine);
    case 'sysbox_missing':
      return words(
        `The task "${cause.task}" runs Docker inside its sandbox, which needs the Sysbox runtime, and ${machine} does not have it. Choose another task or another machine.`,
        cause.problem,
        false,
      );
    case 'config_invalid':
      return words(
        "The run's configuration (the project's agent configuration, and the settings chosen here, such as a different agent for a role) is not valid. Change the setting the detail names, or fix it in the project, then check again.",
        cause.problem,
        true,
      );
    case 'roles_not_declared':
    case 'model_not_vetted':
      return agentWords(cause);
    case 'arguments_rejected':
      return words(
        'The Extra arguments were not accepted. Edit or clear them in Budget and results, then check again.',
        cause.problem,
        false,
      );
    case 'arguments_unrecognized':
      return words(
        'The search these Extra arguments choose does not take some of them. Edit or clear them in Budget and results, then check again.',
        cause.arguments.join(' '),
        false,
      );
    case 'option_not_accepted': {
      const control = OPTION_CONTROLS[cause.option];
      return words(
        `This search does not take ${control.name}. ${control.reset}, or change the search in Extra arguments, then check again.`,
        null,
        false,
      );
    }
    case 'github_unavailable':
      return words(
        `Results are set to publish to GitHub, but ${machine} has no signed-in GitHub account. Sign in to GitHub on that machine, or set Results to keep them there, then check again.`,
        cause.problem,
        true,
      );
    case 'measurement_target_not_configured':
    case 'measurement_target_misfit':
      return measurementWords(cause, machine);
    default:
      // A cause from a helper newer than this app: say so plainly, and keep its facts out of view.
      return genericWords(check);
  }
}
