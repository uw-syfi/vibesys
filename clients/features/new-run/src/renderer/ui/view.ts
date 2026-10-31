/**
 * What the New run screen shows, derived from the machine's snapshot: the facts about the machine,
 * the "Ready to start" items, why Start is disabled, and the one-line summary of the run.
 */
import {acceleratorName, type HostKey, hostFromKey, hostLabel} from '@vibesys/hosts';
import type {MeasureTarget} from '@vibesys/ipc';
import type {RecentRun} from '@vibesys/runs';
import type {BrandMarkName, CheckState, KeyValueItem} from '@vibesys/ui';
import {
  type AgentMaker,
  type AgentPick,
  advancedProblem,
  type Backend,
  type CatalogueEntry,
  type Check,
  catalogueFor,
  checksPass,
  chosenEntry,
  destinationView,
  fitOf,
  harnessLabel,
  inputsKey,
  type MachineDescription,
  type Maker,
  missingFeatures,
  type NewRunContext,
  type NewRunSnapshot,
  offers,
  projectPath,
  startUnconfirmed,
  type Vendor,
} from '../../state/index.js';
import {type FailureWords, failureWords} from './cause-words.js';

export interface MachineOption {
  readonly value: HostKey;
  readonly label: string;
}

/** This machine first, then each of `hosts` (the saved and watched machines) once, by label. */
export function machineOptions(hosts: readonly HostKey[]): MachineOption[] {
  return [...new Set(['local', ...hosts])].map(key => ({value: key, label: machineLabel(key)}));
}

/** The entries of the catalogue of the machine the form is for; empty while it is not known. */
export function entriesIn(context: NewRunContext): readonly CatalogueEntry[] {
  return catalogueFor(context.examples, context.form.machine) ?? [];
}

export function machineLabel(key: HostKey): string {
  return hostLabel(hostFromKey(key));
}

/** The facts the Machine card lists. */
export function machineFacts(machine: MachineDescription): KeyValueItem[] {
  const {hardware, docker, harness_clis, harness_cli_names, runs, checkout} = machine;
  const gpus = hardware.gpus.map(gpu => gpu.model).join(', ');
  return [
    ...(checkout === null
      ? []
      : [{key: 'VibeSys', value: `${checkout.version} at ${checkout.root}`, mono: true}]),
    {
      key: 'Hardware',
      value: `${hardware.cpu_cores} cores${gpus === '' ? '' : `, ${gpus}`} (${acceleratorName(hardware.accelerator)})`,
    },
    {
      key: 'Docker',
      value: 'reason' in docker ? `not reachable: ${docker.reason}` : docker.version,
    },
    {
      key: 'Agent CLIs',
      value:
        harness_clis.length === 0
          ? 'none'
          : harness_clis.map(provider => harness_cli_names?.[provider] ?? provider).join(', '),
    },
    {key: 'Runs', value: `${runs.live} live, ${runs.ended_today} ended today`},
  ];
}

/** The hardware in a line ("64 cores, MI300X"), for the Machine details while they are closed. */
export function hardwareLine(machine: MachineDescription): string {
  const {hardware} = machine;
  const gpus = [...new Set(hardware.gpus.map(gpu => gpu.model))].join(', ');
  return `${hardware.cpu_cores} cores${gpus === '' ? '' : `, ${gpus}`}`;
}

/** A project the user opened before, on the machine it is on. */
export interface RecentChoice {
  readonly host: HostKey;
  readonly path: string;
  /** The folder's name, with enough of the folders above it to tell it from another's. */
  readonly label: string;
}

/** The projects of the runs opened, newest first, each once per machine. */
export function recentChoices(recent: readonly RecentRun[]): RecentChoice[] {
  const ordered = [...recent].sort((left, right) => right.attachedAt - left.attachedAt);
  const seen = new Set<string>();
  const unique = ordered.filter(run => {
    const key = `${run.host}\n${run.project}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
  const labels = new Map(
    [...new Set(unique.map(run => run.host))].map(host => [
      host,
      projectLabels(unique.filter(run => run.host === host).map(run => run.project)),
    ]),
  );
  return unique.map(run => ({
    host: run.host,
    path: run.project,
    label: labels.get(run.host)?.get(run.project) ?? run.project,
  }));
}

/**
 * One label per project path, for a list a person picks from: the folder's name, and when two
 * paths share it, enough of the folders above to tell them apart. A path is never shown whole
 * unless it must be to differ from another.
 */
export function projectLabels(paths: readonly string[]): ReadonlyMap<string, string> {
  const folders = (path: string): string[] => path.split('/').filter(Boolean);
  const nameOf = (path: string): string => folders(path).at(-1) ?? path;
  /** `path` with its last `depth` folders: the name, then the folders above it in parentheses. */
  const labelAt = (path: string, depth: number): string => {
    const parts = folders(path);
    const parents = parts.slice(Math.max(0, parts.length - depth), -1);
    return parents.length === 0 ? nameOf(path) : `${nameOf(path)} (${parents.join('/')})`;
  };
  const groups = new Map<string, string[]>();
  for (const path of new Set(paths)) {
    groups.set(nameOf(path), [...(groups.get(nameOf(path)) ?? []), path]);
  }
  const labels = new Map<string, string>();
  for (const [name, group] of groups) {
    const [only] = group;
    if (group.length === 1 && only !== undefined) {
      labels.set(only, name);
      continue;
    }
    const deepest = Math.max(...group.map(path => folders(path).length));
    let depth = 2;
    while (
      depth < deepest &&
      new Set(group.map(path => labelAt(path, depth))).size < group.length
    ) {
      depth += 1;
    }
    const distinct = new Set(group.map(path => labelAt(path, depth))).size === group.length;
    for (const path of group) labels.set(path, distinct ? labelAt(path, depth) : path);
  }
  return labels;
}

/**
 * The mark drawn beside a harness's name, or null for the generic glyph. The marks are
 * `simple-icons`'s, shown only beside the product's name to say which product runs; a harness
 * the package has no mark for (Codex today) has none.
 */
export function harnessMark(provider: string): BrandMarkName | null {
  switch (provider) {
    case 'claude':
      return 'claudecode';
    case 'gemini':
      return 'googlegemini';
    case 'opencode':
      return 'opencode';
    default:
      return null;
  }
}

/** The mark of a maker of served models, or null: `simple-icons` has none for the rest, which show their name alone. */
const MODEL_MAKER_MARKS: Readonly<Record<Maker, BrandMarkName | null>> = {
  meta: 'meta',
  qwen: null,
  deepseek: 'deepseek',
  zhipu: null,
  moonshot: 'moonshotai',
  ai2: null,
  openai: null,
  other: null,
};

export function modelMakerMark(maker: Maker): BrandMarkName | null {
  return MODEL_MAKER_MARKS[maker];
}

/** The mark of a maker of the agent's own models, or null for one `simple-icons` has none for. */
export function agentMakerMark(maker: AgentMaker | null): BrandMarkName | null {
  switch (maker) {
    case 'anthropic':
      return 'anthropic';
    case 'google':
      return 'google';
    case 'openai':
    case null:
      return null;
  }
}

const VENDOR_MARKS: Readonly<Record<Vendor, BrandMarkName | null>> = {
  nvidia: 'nvidia',
  amd: 'amd',
  aws: null,
  apple: 'apple',
  cpu: null,
};

/** The mark of a hardware vendor, or null: AWS and a plain CPU have none to draw. */
export function vendorMark(vendor: Vendor): BrandMarkName | null {
  return VENDOR_MARKS[vendor];
}

/**
 * What the Agent card shows of the roles: the table of the run's roles, or why there is none. The
 * roles are the run's orchestration's, read with the checks (`run_roles`); a machine whose helper
 * does not report them keeps every role as the run.
 */
export type RolesView =
  | {readonly kind: 'ready'; readonly roles: readonly string[]}
  | {readonly kind: 'pending'}
  | {readonly kind: 'unsupported'};

export function rolesView(context: NewRunContext): RolesView {
  if (context.roles !== null) return {kind: 'ready', roles: context.roles};
  const known = context.helper?.for === context.form.machine;
  return known && !offers(context.helper, context.form.machine, 'run_roles')
    ? {kind: 'unsupported'}
    : {kind: 'pending'};
}

/**
 * The compute backend the run will use, read-only: it is the task's, so it is never chosen.
 * Null until the checks for the inputs now have reported it (or from a helper that does not).
 */
export function backendOf(context: NewRunContext): Backend | null {
  const key = inputsKey(context.form);
  return key !== null && context.checks?.for === key ? context.checks.backend : null;
}

const CHECK_LABELS: Readonly<Record<Check['id'], string>> = {
  checkout_found: 'VibeSys checkout',
  project_own_repository: 'Project in its own repository',
  project_clean: 'Project changes',
  settings_valid: 'Run settings',
  docker_reachable: 'Docker',
  agent_image: 'Agent image',
  evaluator_available: 'Evaluator',
  measurement_target: 'Where results are measured',
};

/** One "Ready to start" item, with the fix it offers named but not yet bound to an action. */
export interface Readiness {
  readonly id: string;
  readonly state: CheckState;
  readonly label: string;
  readonly detail?: string;
  /** What the run printed about a failure, for a collapsible detail, never the headline. */
  readonly raw?: string;
  readonly fix?: ReadinessFix;
}

export type ReadinessFix =
  | {readonly kind: 'copy_out'; readonly source: string; readonly destination: string}
  | {readonly kind: 'create_from_example'; readonly example: string; readonly destination: string}
  /** The destination already holds the example's project: use it as it is. */
  | {
      readonly kind: 'use_created';
      readonly example: string;
      readonly destination: string;
      readonly path: string;
    }
  /** The destination is taken: the user picks another name in More options. */
  | {readonly kind: 'rename_destination'}
  | {readonly kind: 'choose_checkout'}
  | {readonly kind: 'recheck'};

function checkFix(
  check: Check,
  project: string | null,
  words: FailureWords | null,
): ReadinessFix | undefined {
  // What a cause asks is done outside this screen (the project, the machine); then the inputs
  // are checked again.
  if (words?.recheck === true) return {kind: 'recheck'};
  const fix = check.fix ?? null;
  if (fix === null) return undefined;
  if (fix.kind === 'copy_out') {
    return project === null
      ? undefined
      : {kind: 'copy_out', source: project, destination: fix.suggested_path};
  }
  if (fix.kind === 'choose_checkout') return {kind: 'choose_checkout'};
  // Docker, settings and a task's missing [build] (declare_build, named in the detail) are
  // changed outside this screen; then the inputs are checked again.
  return {kind: 'recheck'};
}

function checkItem(check: Check, project: string | null, machine: HostKey): Readiness {
  const words = check.state === 'fail' ? failureWords(check, machineLabel(machine)) : null;
  const fix = check.state === 'ok' ? undefined : checkFix(check, project, words);
  const copyTo = fix?.kind === 'copy_out' ? ` Its copy goes to ${fix.destination}.` : '';
  return {
    id: check.id,
    state: check.state,
    label: CHECK_LABELS[check.id],
    detail: `${words?.headline ?? check.detail}${copyTo}`,
    ...(words?.raw == null ? {} : {raw: words.raw}),
    ...(fix === undefined ? {} : {fix}),
  };
}

/** The Project item for an example not made yet: made, used where it already is, or renamed. */
function exampleItem(
  context: NewRunContext,
  chosen: {readonly example: string; readonly destination: string},
): Readiness {
  const {example, destination} = chosen;
  const view = destinationView(context.helper, context.form, context.destination);
  const base = {id: 'project', label: 'Project'} as const;
  if (view?.kind === 'checking') {
    return {...base, state: 'pending', detail: 'Checking the destination'};
  }
  if (view?.kind === 'found' && view.found.state === 'occupied') {
    return {
      ...base,
      state: 'fail',
      detail: `${destination} is taken`,
      fix: {kind: 'rename_destination'},
    };
  }
  if (view?.kind === 'found' && view.found.state === 'example_project' && view.found.project) {
    return {
      ...base,
      state: 'fail',
      detail: `${destination} is already ${example}`,
      fix: {kind: 'use_created', example, destination, path: view.found.project},
    };
  }
  return {
    ...base,
    state: 'fail',
    detail: `Create ${example} as its own repository at ${destination} first.`,
    fix: {kind: 'create_from_example', example, destination},
  };
}

/** Whether the destination of the example chosen holds something that is not its project. */
export function destinationTaken(context: NewRunContext): boolean {
  const view = destinationView(context.helper, context.form, context.destination);
  return view?.kind === 'found' && view.found.state === 'occupied';
}

function missing(context: NewRunContext): Readiness[] {
  const {form} = context;
  const items: Readiness[] = [];
  if (form.project === null) {
    items.push({id: 'project', state: 'pending', label: 'Project', detail: 'Choose a project.'});
  } else if (form.project.source === 'example') {
    items.push(exampleItem(context, form.project));
  }
  if (projectPath(form) !== null && form.task === null) {
    items.push({id: 'task', state: 'pending', label: 'Task', detail: 'Choose a task.'});
  }
  if (form.agent === null) {
    items.push({id: 'agent', state: 'pending', label: 'Agent', detail: 'Choose a harness.'});
  }
  const problem = advancedProblem(form.advanced);
  if (problem !== null) {
    items.push({id: 'advanced', state: 'fail', label: 'Extra arguments', detail: problem});
  }
  return items;
}

/** Said wherever a machine lacks a feature this app needs: its VibeSys is too old. */
export const NEEDS_UPDATE = 'Needs an update';

/** The failed item for a helper without a feature this app needs; null when it has them all. */
function helperItem(context: NewRunContext): Readiness | null {
  const {machine} = context.form;
  if (missingFeatures(context.helper, machine).length === 0) return null;
  return {
    id: 'helper',
    state: 'fail',
    label: `VibeSys on ${machineLabel(machine)}`,
    detail: NEEDS_UPDATE,
  };
}

function hardwareItem(context: NewRunContext, machine: MachineDescription): Readiness | null {
  const needs = chosenEntry(context.answers, entriesIn(context))?.details?.hardware ?? null;
  if (needs === null) return null;
  const fit = fitOf(needs, machine, machineLabel(context.form.machine), context.form.measure);
  if (fit.kind === 'unknown') return null;
  return {
    id: 'hardware_fit',
    state: fit.kind === 'fits' ? 'ok' : 'warn',
    label: 'Hardware fits the task',
    detail: fit.words,
  };
}

function harnessItem(context: NewRunContext, machine: MachineDescription): Readiness | null {
  const {agent} = context.form;
  const offered = context.harnesses?.for === context.form.machine ? context.harnesses.list : null;
  if (agent === null || offered === null) return null;
  const name = machineLabel(context.form.machine);
  const installed = machine.harness_clis.includes(agent.provider);
  const label = harnessLabel(offered, agent.provider);
  return {
    id: 'harness_installed',
    state: installed ? 'ok' : 'warn',
    label: `${label} on ${name}`,
    detail: installed ? `${label} is installed on ${name}.` : `${label} was not found on ${name}.`,
  };
}

/**
 * What the machine's own report says about the run: whether it has the hardware the task targets
 * and the agent's CLI. Both only advise (`warn`): the helper's checks decide whether Start is open.
 */
function machineItems(context: NewRunContext): Readiness[] {
  const facts = context.facts?.for === context.form.machine ? context.facts.value : null;
  if (facts === null || facts.kind !== 'offered') return [];
  return [hardwareItem(context, facts.value), harnessItem(context, facts.value)].flatMap(item =>
    item === null ? [] : [item],
  );
}

/** The "Ready to start" items for the inputs now. */
export function readiness(snapshot: NewRunSnapshot): Readiness[] {
  const helper = helperItem(snapshot.context);
  const items = inputReadiness(snapshot);
  const own = machineItems(snapshot.context);
  if (helper === null) return [...items, ...own];
  // Nothing is checked on such a helper; what the inputs still lack is listed after it.
  return [helper, ...items.filter(item => item.id !== 'checking'), ...own];
}

function inputReadiness(snapshot: NewRunSnapshot): Readiness[] {
  const {context} = snapshot;
  const key = inputsKey(context.form);
  if (key === null) return missing(context);
  if (context.checks !== null && context.checks.for === key) {
    const project = projectPath(context.form);
    return context.checks.list.map(check => checkItem(check, project, context.form.machine));
  }
  return [{id: 'checking', state: 'pending', label: 'Checking these inputs on the machine'}];
}

/** Why Start waits after a start that may have happened; the screen draws it beside Start. */
const UNCONFIRMED_START = 'Check Runs first';

/** Why Start is disabled now, or undefined when it may start. */
export function startBlocked(snapshot: NewRunSnapshot): string | undefined {
  const {context} = snapshot;
  if (snapshot.matches({editing: {action: 'starting'}})) return 'Starting the run.';
  if (snapshot.matches({editing: {action: 'fixing'}})) return 'Making the project.';
  if (startUnconfirmed(context.problem)) return UNCONFIRMED_START;
  if (missingFeatures(context.helper, context.form.machine).length > 0) {
    return `VibeSys on ${machineLabel(context.form.machine)} needs an update`;
  }
  if (checksPass(context.form, context.checks)) return undefined;
  if (readiness(snapshot).some(item => item.state === 'fail')) {
    return 'Fix the items marked above to start.';
  }
  if (inputsKey(context.form) === null) return 'Choose a project, task and agent to start.';
  return 'Checking these inputs.';
}

/** The one-line state of "Ready to start", and whether its checks show by themselves. */
export interface ReadinessStatus {
  readonly state: CheckState;
  readonly text: string;
  /** A failed check shows its words and fix without being asked to. */
  readonly open: boolean;
}

function count(n: number, noun: string): string {
  return `${n} ${noun}${n === 1 ? '' : 's'}`;
}

/** What the one line says of `items`: ready with a count, what is being checked, or what failed. */
export function readinessStatus(items: readonly Readiness[]): ReadinessStatus {
  const of = (state: CheckState): Readiness[] => items.filter(item => item.state === state);
  const failed = of('fail');
  if (failed.length > 0) {
    return {
      state: 'fail',
      text:
        failed.length === 1 ? '1 check needs attention' : `${failed.length} checks need attention`,
      open: true,
    };
  }
  const pending = of('pending')[0];
  if (pending !== undefined) {
    // The waiting-for-the-checks row says what is checked; the line says only that it is.
    const text = pending.id === 'checking' ? 'Checking' : (pending.detail ?? pending.label);
    return {state: 'pending', text, open: false};
  }
  const passed = of('ok').length;
  const warned = of('warn').length;
  if (warned > 0) {
    return {
      state: 'warn',
      text: `Ready · ${count(passed, 'check')} passed, ${warned} to review`,
      open: false,
    };
  }
  return {
    state: 'ok',
    text: passed === 0 ? 'Ready' : `Ready · ${count(passed, 'check')} passed`,
    open: false,
  };
}

/**
 * What More options holds that is not the default, in a line: so a closed disclosure never hides a
 * setting the run will use. "Defaults" when it holds none.
 */
export function moreOptionsSummary(context: NewRunContext): string {
  const {form} = context;
  const changedRoles = (form.agent?.roles ?? []).filter(role => role.effort !== 'default').length;
  const parts = [
    ...(form.budget.timeLimitS === null ? [] : ['time limit']),
    ...(form.budget.results === 'publish_github' ? ['publishes to GitHub'] : []),
    ...(changedRoles === 0 ? [] : [`reasoning effort of ${count(changedRoles, 'role')}`]),
    ...(form.advanced.trim() === '' ? [] : ['extra arguments']),
    ...(form.project?.source === 'example' ? ['example location'] : []),
  ];
  return parts.length === 0 ? 'Defaults' : parts.join(' · ');
}

/** A measurement target as a person reads it: the machine itself, a Slurm cluster by name, Modal. */
export function measureLabel(target: MeasureTarget): string {
  switch (target.kind) {
    case 'same_machine':
      return 'Same machine';
    case 'slurm':
      return `Slurm \u00b7 ${target.cluster}`;
    case 'modal':
      return 'Modal';
  }
}

/** The agent of each role that does not use the run's, in the order of the run's roles. */
export function roleAgentsChanged(
  context: NewRunContext,
): {readonly role: string; readonly agent: AgentPick}[] {
  return (context.form.agent?.roles ?? []).flatMap(role =>
    role.agent === null ? [] : [{role: role.role, agent: role.agent}],
  );
}
