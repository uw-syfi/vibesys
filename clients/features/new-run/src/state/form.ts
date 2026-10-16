/**
 * The New run form as data, and the pure rules over it: the inputs a run starts with, their key
 * (the checks are for exactly one key), what Advanced may not repeat, and the prefill from the last
 * run on a machine.
 */
import type {Check, HelperFeature, MachineDescription} from '@vibesys/contracts/host-helper';
import type {HostKey} from '@vibesys/hosts';
import type {HarnessOffer, Offered, RoleOption, RunInputs} from '@vibesys/ipc';
import type {RecentRun, RunStart} from '@vibesys/runs';

/** Where the project comes from: a recent one, a folder on the machine, or a bundled example. */
export type ProjectChoice =
  | {readonly source: 'recent'; readonly path: string}
  | {readonly source: 'folder'; readonly path: string}
  | {readonly source: 'example'; readonly example: string; readonly destination: string};

export type Effort = RoleOption['reasoning_effort'] & string;

/** One role's model (`null`: the run's) and reasoning effort; every role runs on the run's harness. */
export interface RoleChoice {
  readonly role: string;
  readonly model: string | null;
  readonly effort: Effort;
}

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
  readonly advanced: string;
}

/** The roles the run's agent plays, with what each does, in the order the table lists them. */
export const ROLES: readonly {
  readonly role: string;
  readonly label: string;
  readonly does: string;
}[] = [
  {role: 'orchestrator', label: 'Orchestrator', does: 'Plans each round'},
  {role: 'profiler', label: 'Profiler', does: 'Finds hot spots'},
  {role: 'implementer', label: 'Implementer', does: 'Writes candidates'},
  {role: 'judge', label: 'Judge', does: 'Reviews results'},
];

export const DEFAULT_BUDGET: Budget = {rounds: null, timeLimitS: null, results: 'keep'};

/** A role left at the run's model and the default effort. */
function defaultRole(role: string): RoleChoice {
  return {role, model: null, effort: 'default'};
}

/** The agent `offers` start with: the first provider's default model, every role the same. */
export function defaultAgent(
  offers: readonly HarnessOffer[],
  provider?: string,
): AgentChoice | null {
  const chosen = provider ?? offers[0]?.provider;
  const ofProvider = offers.filter(offer => offer.provider === chosen);
  const model = (ofProvider.find(offer => offer.default) ?? ofProvider[0])?.model;
  if (chosen === undefined || model === undefined) return null;
  return {provider: chosen, model, roles: ROLES.map(({role}) => defaultRole(role))};
}

/** The vetted models of `provider`, in offer order. */
export function vettedModels(offers: readonly HarnessOffer[], provider: string): string[] {
  return offers.filter(offer => offer.provider === provider).map(offer => offer.model);
}

/** The form for `machine`, prefilled from the last run opened on it. */
export function prefill(machine: HostKey, recent: readonly RecentRun[]): Form {
  const last = recent
    .filter(run => run.host === machine)
    .reduce<RecentRun | null>(
      (best, run) => (best === null || run.attachedAt > best.attachedAt ? run : best),
      null,
    );
  return {
    machine,
    project: last === null ? null : {source: 'recent', path: last.project},
    task: last?.task ?? null,
    agent: null,
    budget: DEFAULT_BUDGET,
    advanced: '',
  };
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
    advanced: start === null ? '' : joinWords(start.args),
  };
}

function roleChoice(option: RoleOption): RoleChoice {
  return {
    role: option.role,
    model: option.model ?? null,
    effort: option.reasoning_effort ?? 'default',
  };
}

/**
 * The agent a run was started with, among what `offers` vets: its harness and model when the
 * machine still offers them (else the default), with the role settings it recorded.
 */
export function recordedAgent(
  start: RunStart | null,
  offers: readonly HarnessOffer[],
): AgentChoice | null {
  const harness = start?.harness ?? null;
  const offered =
    harness !== null &&
    offers.some(offer => offer.provider === harness.provider && offer.model === harness.model);
  const base = defaultAgent(offers, offered ? harness.provider : undefined);
  if (base === null) return null;
  const agent = offered ? {...base, model: harness.model} : base;
  const recorded = start?.options.roles ?? [];
  const known = new Set(agent.roles.map(role => role.role));
  const roles = [
    ...agent.roles.map(role => {
      const found = recorded.find(each => each.role === role.role);
      return found === undefined ? role : roleChoice(found);
    }),
    ...recorded.filter(each => !known.has(each.role)).map(roleChoice),
  ];
  return {...agent, roles};
}

/** The flags a typed option sets, which Advanced may not repeat (as `run.start` refuses them). */
const TYPED_FLAGS = [
  '--max-rounds',
  '--max-run-seconds',
  '--local',
  '--publish',
  '--repo',
  '--role-model',
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
        .filter(role => role.model !== null || role.effort !== 'default')
        .map(role => ({role: role.role, model: role.model, reasoning_effort: role.effort})),
    },
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
 * The helper features New run needs: typed run options (`run.start` refuses them otherwise), the
 * Ready to start checks, and the examples. A helper without one predates this app's New run.
 */
export const NEW_RUN_FEATURES: readonly HelperFeature[] = ['run_options', 'preflight', 'examples'];

/** The features New run needs that `machine`'s helper lacks; none while they are not known yet. */
export function missingFeatures(helper: HelperFor | null, machine: HostKey): HelperFeature[] {
  if (helper === null || helper.for !== machine) return [];
  return NEW_RUN_FEATURES.filter(feature => !helper.features.includes(feature));
}

/** Whether `machine`'s helper is known to take `feature`. */
export function offers(
  helper: HelperFor | null,
  machine: HostKey,
  feature: HelperFeature,
): boolean {
  return helper !== null && helper.for === machine && helper.features.includes(feature);
}

/** The checks a run is held to, for one key. */
export interface ChecksFor {
  readonly for: string;
  readonly list: readonly Check[];
}

/** What a machine offers, for one machine. */
export interface FactsFor {
  readonly for: HostKey;
  readonly value: Offered<MachineDescription>;
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
