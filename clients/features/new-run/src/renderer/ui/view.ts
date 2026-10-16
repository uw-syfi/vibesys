/**
 * What the New run screen shows, derived from the machine's snapshot: the facts about the machine,
 * the "Ready to start" items, why Start is disabled, and the one-line summary of the run.
 */
import {type HostKey, hostFromKey, hostLabel} from '@vibesys/hosts';
import type {RecentRun} from '@vibesys/runs';
import type {CheckState, KeyValueItem} from '@vibesys/ui';
import {
  advancedProblem,
  type Check,
  checksPass,
  inputsKey,
  type MachineDescription,
  missingFeatures,
  type NewRunContext,
  type NewRunSnapshot,
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

const CHECK_LABELS: Readonly<Record<Check['id'], string>> = {
  checkout_found: 'VibeSys checkout',
  project_own_repository: 'Project in its own repository',
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
  const fix = check.fix ?? null;
  if (fix === null) return undefined;
  if (fix.kind === 'copy_out') {
    return project === null
      ? undefined
      : {kind: 'copy_out', source: project, destination: fix.suggested_path};
  }
  if (fix.kind === 'choose_checkout') return {kind: 'choose_checkout'};
  // Docker and settings are changed outside this screen; then the inputs are checked again.
  return {kind: 'recheck'};
}

function checkItem(check: Check, project: string | null): Readiness {
  const fix = check.state === 'ok' ? undefined : checkFix(check, project);
  const copyTo = fix?.kind === 'copy_out' ? ` Its copy goes to ${fix.destination}.` : '';
  return {
    id: check.id,
    state: check.state,
    label: CHECK_LABELS[check.id],
    detail: `${check.detail}${copyTo}`,
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
    return context.checks.list.map(check => checkItem(check, project));
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
  return [lastSegment(project), task, machineLabel(machine), agent.model, rounds].join(' · ');
}
