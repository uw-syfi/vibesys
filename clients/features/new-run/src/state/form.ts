/**
 * The New run form as data, and the pure rules over it: the inputs a run starts with, their key
 * (the checks are for exactly one key), what Advanced may not repeat, and the prefill from the last
 * run on a machine.
 */
import type {
  Backend,
  Check,
  HelperFeature,
  MachineDescription,
  TaskGoal,
} from '@vibesys/contracts/host-helper';
import {type HostKey, missingRequiredFeatures} from '@vibesys/hosts';
import type {
  DestinationFound,
  HarnessOffer,
  MeasureTarget,
  Offered,
  RoleOption,
  RunInputs,
} from '@vibesys/ipc';
import type {RecentRun, RunStart} from '@vibesys/runs';
import type {OwnProject} from './answers.js';

/**
 * Where the project comes from: one of the user's (a recent one, or a folder on the machine), an
 * example not made into a project yet, or the project made from an example at `path`.
 */
export type ProjectChoice =
  | OwnProject
  | {readonly source: 'example'; readonly example: string; readonly destination: string}
  | {
      readonly source: 'created';
      readonly example: string;
      readonly destination: string;
      readonly path: string;
    };

export type Effort = RoleOption['reasoning_effort'] & string;

/** A harness and one of the models it runs. */
export interface AgentPick {
  readonly provider: string;
  readonly model: string;
}

/** One role's agent (`null`: the run's, whatever it is chosen to be) and reasoning effort. */
export interface RoleChoice {
  readonly role: string;
  readonly agent: AgentPick | null;
  readonly effort: Effort;
}

/** Where a run's agents execute: the helper refuses roles on mixed providers in containers. */
export type AgentEnvironment = 'container' | 'host';

/**
 * Every run New run starts runs its agents in containers (the host environment is not selectable
 * here yet); the measurement-target choice replaces this with the form's own.
 */
export const AGENT_ENVIRONMENT: AgentEnvironment = 'container';

export interface AgentChoice {
  readonly provider: string;
  readonly model: string;
  readonly roles: readonly RoleChoice[];
}

export interface Budget {
  /**
   * Null keeps the run's configuration. Not every search has rounds (evolve has none); a count set
   * for one that does not fails `settings_valid`, whose detail names it, so nothing here names a
   * search.
   */
  readonly rounds: number | null;
  /** Null for no limit. */
  readonly timeLimitS: number | null;
  readonly results: 'keep' | 'publish_github';
}

export interface Form {
  readonly machine: HostKey;
  readonly project: ProjectChoice | null;
  readonly task: string | null;
  readonly agent: AgentChoice | null;
  readonly budget: Budget;
  /** Where results are measured: this machine, a cluster set up on it, or Modal. */
  readonly measure: MeasureTarget;
  readonly advanced: string;
}

export const SAME_MACHINE: MeasureTarget = {kind: 'same_machine'};

export const DEFAULT_BUDGET: Budget = {rounds: null, timeLimitS: null, results: 'keep'};

/** A role left at the run's agent and the default effort. */
export function defaultRole(role: string): RoleChoice {
  return {role, agent: null, effort: 'default'};
}

/**
 * The agent `offers` start with: the first provider's default model, every one of `roles` (the
 * run's, as its helper reported them) left as the run.
 */
export function defaultAgent(
  offers: readonly HarnessOffer[],
  roles: readonly string[],
  provider?: string,
): AgentChoice | null {
  const chosen = provider ?? offers[0]?.provider;
  const ofProvider = offers.filter(offer => offer.provider === chosen);
  const model = (ofProvider.find(offer => offer.default) ?? ofProvider[0])?.model;
  if (chosen === undefined || model === undefined) return null;
  return {provider: chosen, model, roles: roles.map(defaultRole)};
}

/** The harness as people call it ("Claude Code"): the name the host sends, else the raw id. */
export function harnessLabel(offers: readonly HarnessOffer[], provider: string): string {
  return offers.find(offer => offer.provider === provider)?.display?.providerName ?? provider;
}

/** The model as people call it ("Opus 5.5"): the name the host sends, else the raw id. */
export function modelLabel(
  offers: readonly HarnessOffer[],
  provider: string,
  model: string,
): string {
  const offer = offers.find(each => each.provider === provider && each.model === model);
  return offer?.display?.modelName ?? model;
}

/** The vetted models of `provider`, in offer order. */
export function vettedModels(offers: readonly HarnessOffer[], provider: string): string[] {
  return offers.filter(offer => offer.provider === provider).map(offer => offer.model);
}

/** The last run opened on `machine`, or null when none was. */
export function lastRun(machine: HostKey, recent: readonly RecentRun[]): RecentRun | null {
  return recent
    .filter(run => run.host === machine)
    .reduce<RecentRun | null>(
      (best, run) => (best === null || run.attachedAt > best.attachedAt ? run : best),
      null,
    );
}

/**
 * A form for `machine` that knows no project yet: it carries the budget and extra arguments of the
 * last run opened on it, and its agent follows once the machine's harnesses are known
 * (`recordedAgent`, from `lastRun`'s start).
 */
export function prefill(machine: HostKey, recent: readonly RecentRun[]): Form {
  const last = lastRun(machine, recent);
  if (last === null) {
    return {
      machine,
      project: null,
      task: null,
      agent: null,
      budget: DEFAULT_BUDGET,
      measure: SAME_MACHINE,
      advanced: '',
    };
  }
  return {...againForm(last), machine, project: null, task: null};
}

/** `words` as one line of Advanced text, quoting a word that has spaces (`splitWords` reads it back). */
export function joinWords(words: readonly string[]): string {
  return words
    .map(word => {
      if (!/\s/.test(word) && word !== '') return word;
      return word.includes('"') ? `'${word}'` : `"${word}"`;
    })
    .join(' ');
}

/**
 * The form for running `run` again: its machine, project and task, and the budget and extra
 * arguments it was started with. The agent follows once the machine's harnesses are known
 * (`recordedAgent`); a run the app did not start has no record, so only its place is kept.
 */
export function againForm(run: RecentRun): Form {
  const start = run.start ?? null;
  const options = start?.options;
  return {
    machine: run.host,
    project: {source: 'recent', path: run.project},
    task: run.task,
    agent: null,
    budget: {
      rounds: options?.rounds ?? null,
      timeLimitS: options?.time_limit_s ?? null,
      results: options?.results ?? 'keep',
    },
    measure: start?.measure_on ?? SAME_MACHINE,
    advanced: start === null ? '' : joinWords(start.args),
  };
}

/** The roles a run was started with a setting for: the agent each had and its reasoning effort. */
function recordedRoles(start: RunStart | null): RoleChoice[] {
  const efforts = new Map<string, Effort>(
    (start?.options.roles ?? []).map(option => [option.role, option.reasoning_effort ?? 'default']),
  );
  const names = [...new Set([...efforts.keys(), ...Object.keys(start?.agents ?? {})])];
  return names.map(role => ({
    role,
    agent: start?.agents[role] ?? null,
    effort: efforts.get(role) ?? 'default',
  }));
}

/** Whether `offers` still vet the harness and model `start` recorded. */
export function recordedHarnessOffered(
  start: RunStart | null,
  offers: readonly HarnessOffer[],
): boolean {
  const harness = start?.harness ?? null;
  return (
    harness !== null &&
    offers.some(offer => offer.provider === harness.provider && offer.model === harness.model)
  );
}

/**
 * The agent a run was started with, among what `offers` vets: its harness and model when the
 * machine still offers them (else the default), with the role settings it recorded.
 */
export function recordedAgent(
  start: RunStart | null,
  offers: readonly HarnessOffer[],
): AgentChoice | null {
  const harness = recordedHarnessOffered(start, offers) ? (start?.harness ?? null) : null;
  // The roles the run reports are not known yet: keep the recorded ones, which the run's own
  // roles replace once reported (`formWithRoles`).
  const base = defaultAgent(offers, [], harness?.provider);
  if (base === null) return null;
  const agent = harness === null ? base : {...base, model: harness.model};
  return {...agent, roles: recordedRoles(start)};
}

/** The flags a typed option sets, which Advanced may not repeat (as `run.start` refuses them). */
const TYPED_FLAGS = [
  '--max-rounds',
  '--max-run-seconds',
  '--local',
  '--publish',
  '--repo',
  '--role-model',
  '--role-provider',
  '--role-reasoning-effort',
  '--cli-provider',
  '--model',
];

/** `text` split into words: whitespace separates, single or double quotes group. */
export function splitWords(text: string): string[] {
  const words: string[] = [];
  let word = '';
  let quote: string | null = null;
  let started = false;
  for (const char of text) {
    if (quote !== null) {
      if (char === quote) quote = null;
      else word += char;
    } else if (char === '"' || char === "'") {
      quote = char;
      started = true;
    } else if (/\s/.test(char)) {
      if (started) words.push(word);
      word = '';
      started = false;
    } else {
      word += char;
      started = true;
    }
  }
  if (started) words.push(word);
  return words;
}

/** Why `advanced` cannot start a run, or null: an open quote, or a flag a typed option sets. */
export function advancedProblem(advanced: string): string | null {
  const quotes = [...advanced].filter(char => char === '"' || char === "'").length;
  if (quotes % 2 === 1) return 'A quote is not closed.';
  for (const word of splitWords(advanced)) {
    const flag = word.split('=')[0] ?? word;
    const repeated = TYPED_FLAGS.find(
      typed => typed.startsWith(flag) && flag.startsWith('--') && flag.length > 2,
    );
    if (repeated !== undefined)
      return `${repeated} is set above; remove it from the extra arguments.`;
  }
  return null;
}

/** The project path a run would use now, or null before there is one on the machine. */
export function projectPath(form: Form): string | null {
  if (form.project === null || form.project.source === 'example') return null;
  return form.project.path;
}

/** The inputs a run starts with, or null while the form cannot start one. */
export function runInputs(form: Form): RunInputs | null {
  const project = projectPath(form);
  if (project === null || form.task === null || form.agent === null) return null;
  if (advancedProblem(form.advanced) !== null) return null;
  const {agent, budget} = form;
  return {
    project,
    task: form.task,
    harness: {provider: agent.provider, model: agent.model},
    options: {
      ...(budget.rounds === null ? {} : {rounds: budget.rounds}),
      ...(budget.timeLimitS === null ? {} : {time_limit_s: budget.timeLimitS}),
      results: budget.results,
      roles: agent.roles
        .filter(role => role.effort !== 'default')
        .map(role => ({role: role.role, model: null, reasoning_effort: role.effort})),
    },
    measure_on: form.measure,
    agents: Object.fromEntries(
      agent.roles.flatMap(role => (role.agent === null ? [] : [[role.role, role.agent] as const])),
    ),
    args: splitWords(form.advanced),
  };
}

/** The key of the inputs: checks for one key are not shown for another. */
export function inputsKey(form: Form): string | null {
  const inputs = runInputs(form);
  return inputs === null ? null : JSON.stringify([form.machine, inputs]);
}

/** What one machine's helper agreed to in `hello`. */
export interface HelperFor {
  readonly for: HostKey;
  readonly features: readonly HelperFeature[];
}

/**
 * The features this app needs that `machine`'s helper lacks (`REQUIRED_HELPER_FEATURES`, the one
 * rule every screen uses); none while they are not known yet.
 */
export function missingFeatures(
  helper: HelperFor | null,
  machine: HostKey,
): readonly HelperFeature[] {
  if (helper === null || helper.for !== machine) return [];
  return missingRequiredFeatures(helper.features);
}

/** Whether `machine`'s helper is known to take `feature`. */
export function offers(
  helper: HelperFor | null,
  machine: HostKey,
  feature: HelperFeature,
): boolean {
  return helper !== null && helper.for === machine && helper.features.includes(feature);
}

/** A task's goal as its machine's helper read it, for the project and task it was asked of. */
export interface GoalFor {
  /** `goalKey` of the machine, project and task asked. */
  readonly for: string;
  readonly value: Offered<TaskGoal>;
}

/** The key a goal is read for: the machine, the user's project on it, and the task. */
export function goalKey(machine: HostKey, project: string, task: string): string {
  return JSON.stringify([machine, project, task]);
}

/** What an example's destination held when its machine's helper was asked, for one key. */
export interface DestinationFor {
  /** `destinationKey` of the machine, example and destination asked. */
  readonly for: string;
  /** What it held; null when the helper could not say or was not asked. */
  readonly found: DestinationFound | null;
}

/** The key a destination is read for: the machine, the example and where it would be copied. */
export function destinationKey(form: Form): string | null {
  const {project} = form;
  return project?.source === 'example'
    ? JSON.stringify([form.machine, project.example, project.destination])
    : null;
}

/** What is known of the destination of the example the form has chosen, not yet made. */
export type DestinationView =
  | {readonly kind: 'checking'}
  /** The helper cannot say (too old, or the question failed): creating is the way to find out. */
  | {readonly kind: 'unknown'}
  | {readonly kind: 'found'; readonly found: DestinationFound};

/** The destination of the form's example as `helper` and `destination` say; null for any other project. */
export function destinationView(
  helper: HelperFor | null,
  form: Form,
  destination: DestinationFor | null,
): DestinationView | null {
  const key = destinationKey(form);
  if (key === null) return null;
  if (helper === null || helper.for !== form.machine) return {kind: 'checking'};
  if (!helper.features.includes('example_destination')) return {kind: 'unknown'};
  if (destination?.for !== key) return {kind: 'checking'};
  return destination.found === null ? {kind: 'unknown'} : {kind: 'found', found: destination.found};
}

/** The checks a run is held to, for one key. */
export interface ChecksFor {
  readonly for: string;
  readonly list: readonly Check[];
  /** The compute backend the run resolved to (its task's), when the helper reported it. */
  readonly backend: Backend | null;
}

/** What a machine offers, for one machine. */
export interface FactsFor {
  readonly for: HostKey;
  readonly value: Offered<MachineDescription>;
}

/**
 * The harnesses the machine says it has installed (its description's `harness_clis`), or null when
 * it has not said: not described yet, a helper that cannot describe, or a description that failed.
 */
export function installedHarnesses(
  facts: FactsFor | null,
  machine: HostKey,
): readonly string[] | null {
  return facts?.for === machine && facts.value.kind === 'offered'
    ? facts.value.value.harness_clis
    : null;
}

/** Whether `checks` are for the form's inputs now and none fails: then Start is enabled. */
export function checksPass(form: Form, checks: ChecksFor | null): boolean {
  const key = inputsKey(form);
  return (
    key !== null &&
    checks !== null &&
    checks.for === key &&
    checks.list.every(check => check.state !== 'fail')
  );
}
