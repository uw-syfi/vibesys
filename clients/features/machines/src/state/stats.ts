/**
 * What the Machines page shows of a host's live stats: the subscription's state as a few gauges,
 * a stale value or a status word. Labels, numbers and status words only; each is a function of the
 * subscription's state and never stored.
 */
import {
  FAILURE_WORDS,
  type Instant,
  type MachineReadings,
  type MachineStats,
  machineReadings,
  type SubscriptionState,
} from '@vibesys/subscription';

export {FAILURE_WORDS};

/** A reading is drawn hot from here: the share of its capacity in use. */
const HOT_SHARE = 0.9;

/** One reading of a gauge: its label, how full it is (null: no reading), and its figure. */
export interface GaugeView {
  readonly label: 'CPU' | 'Memory' | 'GPU' | 'VRAM';
  readonly share: number | null;
  readonly figure: string;
  readonly tone: 'ok' | 'accent';
}

export type StatsView =
  /** No value yet: the word says how far the host has come. */
  | {readonly kind: 'waiting'; readonly word: string}
  /** Values; `stale` when the host stopped delivering, with when the last one arrived. */
  | {
      readonly kind: 'values';
      readonly gauges: readonly GaugeView[];
      readonly stale: {readonly valueAt: Instant} | null;
    }
  /** No value and no stream: the word says why, and `retry` whether a press can help. */
  | {readonly kind: 'failed'; readonly word: string; readonly retry: boolean};

const UNREADABLE = '–';
const GIB = 2 ** 30;

function tone(share: number): GaugeView['tone'] {
  return share >= HOT_SHARE ? 'accent' : 'ok';
}

function percent(value: number): string {
  return `${Math.round(value)}%`;
}

/** `used / total GB`, with a decimal while the total is small enough for one to matter. */
function gigabytes(used: number, total: number): string {
  const digits = total >= 100 * GIB ? 0 : 1;
  return `${(used / GIB).toFixed(digits)} / ${(total / GIB).toFixed(digits)} GB`;
}

function share(used: number, total: number): number {
  return total > 0 ? Math.min(1, used / total) : 0;
}

function cpuGauge({cpuPercent}: MachineReadings): GaugeView {
  if (cpuPercent === null) return {label: 'CPU', share: null, figure: UNREADABLE, tone: 'ok'};
  return {
    label: 'CPU',
    share: cpuPercent / 100,
    figure: percent(cpuPercent),
    tone: tone(cpuPercent / 100),
  };
}

function memoryGauge({memory}: MachineReadings): GaugeView {
  if (memory === null) return {label: 'Memory', share: null, figure: UNREADABLE, tone: 'ok'};
  const full = share(memory.usedBytes, memory.totalBytes);
  return {
    label: 'Memory',
    share: full,
    figure: gigabytes(memory.usedBytes, memory.totalBytes),
    tone: tone(full),
  };
}

/** The GPUs as one gauge of their mean use and one of their memory; none for a host without. */
function gpuGauges({gpus: reading}: MachineReadings): readonly GaugeView[] {
  // A host with no such tool has no GPU to read; any other absence is a reading missed.
  if (reading.kind === 'no-tool') return [];
  if (reading.kind === 'unreadable')
    return [{label: 'GPU', share: null, figure: UNREADABLE, tone: 'ok'}];
  const {gpus} = reading;
  if (gpus.length === 0) return [];
  const mean = gpus.reduce((sum, gpu) => sum + gpu.utilizationPercent, 0) / gpus.length;
  const used = gpus.reduce((sum, gpu) => sum + gpu.usedBytes, 0);
  const total = gpus.reduce((sum, gpu) => sum + gpu.totalBytes, 0);
  return [
    {label: 'GPU', share: mean / 100, figure: percent(mean), tone: tone(mean / 100)},
    {
      label: 'VRAM',
      share: share(used, total),
      figure: gigabytes(used, total),
      tone: tone(share(used, total)),
    },
  ];
}

/** The gauges of one sample: CPU, memory, and the GPUs when the host has any. */
export function statsGauges(stats: MachineStats): readonly GaugeView[] {
  const readings = machineReadings(stats);
  return [cpuGauge(readings), memoryGauge(readings), ...gpuGauges(readings)];
}

/** How a screen shows `state`. */
export function statsView(state: SubscriptionState<MachineStats>): StatsView {
  switch (state.kind) {
    case 'connecting':
      return {
        kind: 'waiting',
        word: state.attempt > 0 ? 'Retrying' : state.step === 'stream' ? 'Reading' : 'Connecting',
      };
    case 'live':
      return {kind: 'values', gauges: statsGauges(state.value), stale: null};
    case 'stale':
      return {
        kind: 'values',
        gauges: statsGauges(state.value),
        stale: {valueAt: state.valueAt},
      };
    case 'failed':
      return {kind: 'failed', word: FAILURE_WORDS[state.reason], retry: state.reason !== 'closed'};
  }
}

/** How old a value is, in the largest whole unit: `42 s`, `3 min`, `2 h`. Never negative. */
export function ageWord(now: Instant, valueAt: Instant): string {
  const seconds = Math.max(0, Math.floor((now - valueAt) / 1000));
  if (seconds < 60) return `${seconds} s`;
  const minutes = Math.floor(seconds / 60);
  return minutes < 60 ? `${minutes} min` : `${Math.floor(minutes / 60)} h`;
}
