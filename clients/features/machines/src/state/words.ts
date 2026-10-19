/**
 * How the Machines screen words a machine: its status, the one line under its name, and its facts,
 * each a function of what its last report said.
 */
import {acceleratorName, type MachineDescription, problemWord} from '@vibesys/hosts';
import {gpuLine} from './hardware.js';
import {type Machine, type MachineStatus, machineStatus} from './machines.js';

/** The tones the screen draws a status in (the design system's status tones). */
export type StatusTone = 'neutral' | 'ok' | 'warn' | 'err';

const TONES: Readonly<Record<MachineStatus, StatusTone>> = {
  ready: 'ok',
  'needs-update': 'warn',
  'checkout-not-found': 'warn',
  'sign-in': 'warn',
  unavailable: 'err',
  checking: 'neutral',
};

export function statusWord(status: MachineStatus): string {
  switch (status) {
    case 'ready':
      return 'Ready';
    case 'checking':
      return 'Checking';
    default:
      return problemWord(status);
  }
}

export function statusTone(status: MachineStatus): StatusTone {
  return TONES[status];
}

function facts(machine: Machine): MachineDescription | null {
  const report = machine.report;
  return report?.kind === 'ready' || report?.kind === 'needs-update' ? report.facts : null;
}

/** The line under a machine's name: its checkout, then what is most worth knowing. */
export function machineSummary(machine: Machine): string {
  const described = facts(machine);
  switch (machineStatus(machine)) {
    case 'needs-update':
      return `${machine.checkout} · needs an update`;
    case 'ready': {
      if (described === null) return machine.checkout;
      const version = described.checkout?.version;
      const parts = [machine.checkout, ...(version ? [version] : [])];
      return [...parts, `${described.hardware.cpu_cores} CPU`].join(' · ');
    }
    case 'checkout-not-found':
      return `${machine.checkout} · missing`;
    case 'sign-in':
      return 'Waiting for sign-in';
    case 'unavailable':
      return `${machine.checkout} · can’t connect`;
    case 'checking':
      return `${machine.checkout} · checking`;
  }
}

function versionText(described: MachineDescription): string {
  const checkout = described.checkout;
  if (checkout === null) return 'an installed VibeSys';
  const commit = checkout.commit === null ? [] : [checkout.commit.slice(0, 7)];
  const state = checkout.dirty === null ? [] : [checkout.dirty ? 'local changes' : 'clean'];
  return [checkout.version, ...commit, ...state].join(' · ');
}

function hardwareText(described: MachineDescription): string {
  const {cpu_cores, gpus, accelerator} = described.hardware;
  const counts = new Map<string, number>();
  for (const gpu of gpus) counts.set(gpu.model, (counts.get(gpu.model) ?? 0) + 1);
  const shown = [...counts].map(([model, count]) => gpuLine(count, [model]));
  const graphics =
    shown.length === 0 ? 'no GPU' : `${shown.join(', ')} (${acceleratorName(accelerator)})`;
  return `${cpu_cores} CPU cores · ${graphics}`;
}

function dockerText(described: MachineDescription): string {
  const {docker} = described;
  return 'version' in docker ? `Docker ${docker.version}` : `not reachable: ${docker.reason}`;
}

function harnessText(described: MachineDescription): string {
  const names = described.harness_clis.map(
    provider => described.harness_cli_names?.[provider] ?? provider,
  );
  return names.length === 0 ? 'none installed' : `${names.join(' · ')} installed`;
}

function runsText(described: MachineDescription): string {
  const {live, ended_today} = described.runs;
  const parts = [
    ...(live > 0 ? [`${live} live`] : []),
    ...(ended_today > 0 ? [`${ended_today} ended today`] : []),
  ];
  return parts.length === 0 ? 'none today' : parts.join(' · ');
}

/** One fact: its label, its value, and whether the value is set in mono. */
export interface MachineFact {
  readonly key: string;
  readonly value: string;
  readonly mono: boolean;
}

/** The facts the detail lists for `machine`, from its last report. */
export function machineFacts(machine: Machine): MachineFact[] {
  const status = machineStatus(machine);
  const missing = status === 'checkout-not-found';
  const checkout = {
    key: 'Checkout',
    value: missing ? `${machine.checkout} (not found)` : machine.checkout,
    mono: true,
  };
  const described = facts(machine);
  if (described !== null) {
    return [
      checkout,
      {key: 'Version', value: versionText(described), mono: true},
      {key: 'Hardware', value: hardwareText(described), mono: false},
      {key: 'Docker', value: dockerText(described), mono: false},
      {key: 'Harnesses', value: harnessText(described), mono: false},
      {key: 'Runs', value: runsText(described), mono: false},
    ];
  }
  if (missing) return [checkout, {key: 'Version', value: '–', mono: true}];
  const report = machine.report;
  if (
    report !== null &&
    report.kind !== 'ready' &&
    report.kind !== 'needs-update' &&
    machine.checking === null
  ) {
    const connection = report.kind === 'sign-in' ? 'waiting for sign-in' : report.detail;
    return [checkout, {key: 'Connection', value: connection, mono: false}];
  }
  return [checkout];
}

/** What the detail's notice says about a machine that is not ready, or null when it is. */
export function machineProblem(machine: Machine): string | null {
  const report = machine.report;
  if (report === null || machine.checking !== null) return null;
  switch (report.kind) {
    case 'ready':
      return null;
    case 'needs-update':
      return `VibeSys on ${machine.label} needs an update`;
    case 'checkout-not-found':
      return `Nothing is at ${machine.checkout} on ${machine.label} any more. Point the app at your VibeSys checkout there.`;
    case 'sign-in':
    case 'unavailable':
      return `The app couldn’t connect: ${report.detail}`;
  }
}
