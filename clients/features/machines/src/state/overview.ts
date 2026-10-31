/**
 * What the Machines page shows of each machine: a card (its state, hardware, how busy it is) and
 * the rows nested under it, one per cluster or cloud its own settings name. Labels, numbers and
 * status words only; each is a function of the store's state, never stored.
 */
import {acceleratorName, type MachineProblem, mostUrgentProblem, problemWord} from '@vibesys/hosts';
import type {SlurmClusterSettings} from '@vibesys/ipc';
import {gpuLine} from './hardware.js';
import {type Machine, type MachinesState, machineStatus} from './machines.js';
import {clustersOf, type LastCheck, type Places, unreachableClusters} from './places.js';
import {type StatusTone, statusTone} from './words.js';

type Hardware = NonNullable<SlurmClusterSettings['hardware']>;

/** The word and tone a machine's card carries: online, unreachable, or what it waits on. */
export function cardStatus(machine: Machine): {readonly word: string; readonly tone: StatusTone} {
  const status = machineStatus(machine);
  switch (status) {
    case 'ready':
      return {word: 'Online', tone: 'ok'};
    case 'checking':
      return {word: 'Checking', tone: 'neutral'};
    default:
      return {word: problemWord(status), tone: statusTone(status)};
  }
}

function facts(machine: Machine) {
  return machine.report?.kind === 'ready' ? machine.report.facts : null;
}

/** A machine's processors as one line: the cores, then each GPU model with its count; null until known. */
export function machineHardware(machine: Machine): string | null {
  const described = facts(machine);
  if (described === null) return null;
  const counts = new Map<string, number>();
  for (const gpu of described.hardware.gpus) {
    counts.set(gpu.model, (counts.get(gpu.model) ?? 0) + 1);
  }
  const gpus = [...counts].map(([model, count]) => gpuLine(count, [model]));
  return [`${described.hardware.cpu_cores} CPU`, ...gpus].join(' · ');
}

/** What a machine's card offers to fix its connection: Sign in, Reconnect, or nothing. */
export function cardAction(machine: Machine): 'sign-in' | 'reconnect' | null {
  switch (machineStatus(machine)) {
    case 'sign-in':
      return 'sign-in';
    case 'unavailable':
      return 'reconnect';
    case 'ready':
    case 'checking':
    case 'needs-update':
    case 'checkout-not-found':
      return null;
  }
}

/** How busy a machine is: its live runs against a slot for each GPU (one for a machine without). */
export interface Load {
  readonly live: number;
  readonly slots: number;
  /** Live runs over slots, within 0 and 1. */
  readonly share: number;
}

/**
 * The machine's load: the `live` runs its run list holds against the slots its facts count, or
 * null while either is not known.
 */
export function machineLoad(machine: Machine, live: number | null): Load | null {
  const described = facts(machine);
  if (described === null || live === null) return null;
  const slots = Math.max(1, described.hardware.gpus.length);
  return {live, slots, share: Math.min(1, live / slots)};
}

function two(value: number): string {
  return String(value).padStart(2, '0');
}

/** A time of day as the person's clock reads it: `14:03`. */
export function clockWord(at: number): string {
  const time = new Date(at);
  return `${two(time.getHours())}:${two(time.getMinutes())}`;
}

/** What a cluster's nodes offer in a few characters: GPUs per node and their models, else the accelerator. */
export function hardwareLine(hardware: Hardware | null | undefined): string {
  if (hardware === null || hardware === undefined) return '–';
  const models = hardware.gpu_models ?? [];
  const perNode = hardware.gpus_per_node ?? 0;
  if (models.length > 0) return gpuLine(perNode, models);
  if (perNode > 0) return `${perNode} GPU`;
  return hardware.accelerator === null || hardware.accelerator === undefined
    ? '–'
    : acceleratorName(hardware.accelerator);
}

/** One cluster or cloud nested under a machine. */
export interface PlaceRow {
  readonly key: string;
  readonly name: string;
  /** How it is reached: `Slurm over ssh`, `Slurm login node` or `Modal`. */
  readonly reach: string;
  readonly hardware: string;
  /** When it was last checked, or since when it is unreachable, as words. */
  readonly checked: {readonly word: string; readonly tone: StatusTone};
  /** Its test is under way. */
  readonly testing: boolean;
  /** Whether Test applies: a cluster can be tested, a cloud has no test. */
  readonly testable: boolean;
}

function checkedWords(last: LastCheck | null): {readonly word: string; readonly tone: StatusTone} {
  if (last === null) return {word: 'Not checked', tone: 'neutral'};
  if (last.kind === 'passed') return {word: `Checked ${clockWord(last.at)}`, tone: 'ok'};
  return {word: `Unreachable since ${clockWord(last.since)}`, tone: 'err'};
}

function clusterRow(places: Places, cluster: SlurmClusterSettings): PlaceRow {
  const check = places.checks[cluster.name];
  const last = check?.last ?? null;
  const measured = last?.kind === 'passed' ? last.hardware : null;
  return {
    key: `cluster:${cluster.name}`,
    name: cluster.name,
    reach: cluster.reach.kind === 'ssh' ? 'Slurm over ssh' : 'Slurm login node',
    hardware: hardwareLine(cluster.hardware ?? measured),
    checked: checkedWords(last),
    testing: (check?.testing ?? null) !== null,
    testable: true,
  };
}

/**
 * The rows under `machine`: the clusters its settings list, then Modal when its token is found.
 * Nothing is listed for a machine that is not online (it cannot be asked), or not read yet.
 */
export function placeRows(state: MachinesState, machine: Machine): readonly PlaceRow[] {
  const status = machineStatus(machine);
  const places = state.places[machine.host];
  if (places === undefined || (status !== 'ready' && status !== 'checking')) return [];
  const rows = clustersOf(places).map(cluster => clusterRow(places, cluster));
  const offered = places.reading.kind === 'ready' ? places.reading.offered : null;
  if (offered?.kind === 'offered' && offered.value.modal.token_present) {
    rows.push({
      key: 'cloud:modal',
      name: 'Modal',
      reach: 'Modal',
      hardware: '–',
      checked: {word: 'Token found', tone: 'ok'},
      testing: false,
      testable: false,
    });
  }
  return rows;
}

/** Whether a machine's settings could be read, were refused, or are still awaited, for its card. */
export function placesReading(
  state: MachinesState,
  machine: Machine,
): 'loading' | 'ready' | 'unsupported' | 'failed' | 'none' {
  const places = state.places[machine.host];
  if (places === undefined) return 'none';
  if (places.reading.kind === 'loading') return 'loading';
  if (places.reading.kind === 'failed') return 'failed';
  return places.reading.offered.kind === 'offered' ? 'ready' : 'unsupported';
}

/**
 * What the page lists that needs the user, as the most urgent problem: a machine that is not online
 * (and not being checked) names its own status, and a cluster whose last test found it unreachable
 * is `unavailable`. Null when nothing needs the user. The rail's warning dot, worded by
 * `problemWord`, so an old machine reads "Needs an update" there as it does on the page.
 */
export function machinesAttention(state: MachinesState): MachineProblem | null {
  const problems = state.machines.flatMap(each => {
    const status = machineStatus(each);
    return status === 'ready' || status === 'checking' ? [] : [status];
  });
  const clusters = unreachableClusters({places: state.places, lastToken: state.lastToken}) > 0;
  return mostUrgentProblem(clusters ? [...problems, 'unavailable'] : problems);
}
