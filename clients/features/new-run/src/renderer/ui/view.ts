/**
 * What the New run screen shows, derived from the machine's snapshot: the facts about the machine,
 * the "Ready to start" items, why Start is disabled, and the one-line summary of the run.
 */
import {type HostKey, hostFromKey, hostLabel} from '@vibesys/hosts';
import type {RecentRun} from '@vibesys/runs';
import type {CheckState, KeyValueItem} from '@vibesys/ui';
import {
  advancedProblem,
  type Backend,
  type Check,
  checksPass,
  inputsKey,
  type MachineDescription,
  missingFeatures,
  type NewRunContext,
  type NewRunSnapshot,
  offers,
  projectPath,
} from '../../state/index.js';

export interface MachineOption {
  readonly value: HostKey;
  readonly label: string;
}

/** This machine first, then each of `hosts` (the saved and watched machines) once, by label. */
export function machineOptions(hosts: readonly HostKey[]): MachineOption[] {
  return [...new Set(['local', ...hosts])].map(key => ({value: key, label: machineLabel(key)}));
}

export function machineLabel(key: HostKey): string {
  return hostLabel(hostFromKey(key));
}

/** The facts the Machine card lists. */
export function machineFacts(machine: MachineDescription): KeyValueItem[] {
  const {hardware, docker, harness_clis, runs, checkout} = machine;
  const gpus = hardware.gpus.map(gpu => gpu.model).join(', ');
  return [
    ...(checkout === null
      ? []
      : [{key: 'VibeSys', value: `${checkout.version} at ${checkout.root}`, mono: true}]),
    {
      key: 'Hardware',
      value: `${hardware.cpu_cores} cores${gpus === '' ? '' : `, ${gpus}`} (${hardware.accelerator})`,
    },
    {
      key: 'Docker',
      value: 'reason' in docker ? `not reachable: ${docker.reason}` : docker.version,
    },
    {key: 'Agent CLIs', value: harness_clis.length === 0 ? 'none' : harness_clis.join(', ')},
    {key: 'Runs', value: `${runs.live} live, ${runs.ended_today} ended today`},
  ];
}

/** The projects of the runs opened on `machine`, newest first, each once. */
export function recentProjects(recent: readonly RecentRun[], machine: HostKey): string[] {
  const runs = recent
    .filter(run => run.host === machine)
    .sort((left, right) => right.attachedAt - left.attachedAt);
  return [...new Set(runs.map(run => run.project))];
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

/**
 * The compute backend the run will use, read-only: it is the task's, so it is shown, never chosen.
 * Null until the checks for the inputs now have reported it (or from a helper that does not).
 */
export function backendOf(context: NewRunContext): Backend | null {
  const key = inputsKey(context.form);
  return key !== null && context.checks?.for === key ? context.checks.backend : null;
}

/**
 * A failed check in words for this screen, from its typed cause when it has one: the app names
 * what to change here (the task or the machine), never a command-line flag.
 */
/** How many changed paths a failure names before saying how many more there are. */
const SHOWN_PATHS = 5;

function causeDetail(check: Check, machine: HostKey): string | null {
  const cause = check.cause ?? null;
  if (cause === null) return null;
  if (cause.kind === 'project_dirty') {
    const shown = cause.paths.slice(0, SHOWN_PATHS).join(', ');
    const more = cause.paths.length - SHOWN_PATHS;
    return (
      `${lastSegment(cause.project)} has uncommitted changes (${shown}` +
      `${more > 0 ? `, and ${more} more` : ''}). Commit or discard them in the project, then check again.`
    );
  }
  const on = machineLabel(machine);
  const wants =
    cause.declared.length > 1
      ? `one of ${cause.declared.map(backendLabel).join(', ')}`
      : backendLabel(cause.backend);
  return `The task "${cause.task}" runs on ${wants}, which ${on} does not have. Choose another task or another machine.`;
}

const CHECK_LABELS: Readonly<Record<Check['id'], string>> = {
  checkout_found: 'VibeSys checkout',
  project_own_repository: 'Project in its own repository',
  project_clean: 'Project changes',
  settings_valid: 'Run settings',
  docker_reachable: 'Docker',
  agent_image: 'Agent image',
  evaluator_available: 'Evaluator',
};

/** One "Ready to start" item, with the fix it offers named but not yet bound to an action. */
export interface Readiness {
  readonly id: string;
  readonly state: CheckState;
  readonly label: string;
  readonly detail?: string;
  readonly fix?: ReadinessFix;
}

export type ReadinessFix =
  | {readonly kind: 'copy_out'; readonly source: string; readonly destination: string}
  | {readonly kind: 'create_from_example'; readonly example: string; readonly destination: string}
  | {readonly kind: 'choose_checkout'}
  | {readonly kind: 'recheck'};

function checkFix(check: Check, project: string | null): ReadinessFix | undefined {
  // Changes in the project are committed or discarded outside this screen, then checked again.
  if (check.cause?.kind === 'project_dirty') return {kind: 'recheck'};
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
  const fix = check.state === 'ok' ? undefined : checkFix(check, project);
  const copyTo = fix?.kind === 'copy_out' ? ` Its copy goes to ${fix.destination}.` : '';
  return {
    id: check.id,
    state: check.state,
    label: CHECK_LABELS[check.id],
    detail: `${causeDetail(check, machine) ?? check.detail}${copyTo}`,
    ...(fix === undefined ? {} : {fix}),
  };
}

function missing(context: NewRunContext): Readiness[] {
  const {form} = context;
  const items: Readiness[] = [];
  if (form.project === null) {
    items.push({id: 'project', state: 'pending', label: 'Project', detail: 'Choose a project.'});
  } else if (form.project.source === 'example') {
    const {example, destination} = form.project;
    items.push({
      id: 'project',
      state: 'fail',
      label: 'Project',
      detail: `Create ${example} as its own repository at ${destination} first.`,
      fix: {kind: 'create_from_example', example, destination},
    });
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

/** The failed item for a helper older than New run, naming what it lacks; null when it is not. */
function helperItem(context: NewRunContext): Readiness | null {
  const {machine} = context.form;
  const missing = missingFeatures(context.helper, machine);
  if (missing.length === 0) return null;
  const label = machineLabel(machine);
  return {
    id: 'helper',
    state: 'fail',
    label: `VibeSys on ${label}`,
    detail:
      `VibeSys on ${label} is older than this app: update its checkout to start runs from here ` +
      `(it lacks ${missing.join(', ')}).`,
  };
}

/** The "Ready to start" items for the inputs now. */
export function readiness(snapshot: NewRunSnapshot): Readiness[] {
  const helper = helperItem(snapshot.context);
  const items = inputReadiness(snapshot);
  if (helper === null) return items;
  // Nothing is checked on such a helper; what the inputs still lack is listed after it.
  return [helper, ...items.filter(item => item.id !== 'checking')];
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

/** Why Start is disabled now, or undefined when it may start. */
export function startBlocked(snapshot: NewRunSnapshot): string | undefined {
  const {context} = snapshot;
  if (snapshot.matches({editing: {action: 'starting'}})) return 'Starting the run.';
  if (snapshot.matches({editing: {action: 'fixing'}})) return 'Making the project.';
  if (checksPass(context.form, context.checks)) return undefined;
  if (readiness(snapshot).some(item => item.state === 'fail')) {
    return 'Fix the items marked above to start.';
  }
  if (inputsKey(context.form) === null) return 'Choose a project, task and agent to start.';
  return 'Checking these inputs.';
}

function lastSegment(path: string): string {
  return path.split('/').filter(Boolean).at(-1) ?? path;
}

/** The run in one line: project, task, machine, model, rounds; null before it has a project. */
export function summary(context: NewRunContext): string | null {
  const project = projectPath(context.form);
  const {task, agent, budget, machine} = context.form;
  if (project === null || task === null || agent === null) return null;
  const rounds = budget.rounds === null ? 'rounds as configured' : `${budget.rounds} rounds`;
  const backend = backendOf(context);
  return [
    lastSegment(project),
    task,
    machineLabel(machine),
    ...(backend === null ? [] : [backendLabel(backend)]),
    agent.model,
    rounds,
  ].join(' · ');
}
