/**
 * What the Machines page shows of a host's live stats: the subscription's state as a few gauges,
 * a stale value or a status word. Labels, numbers and status words only; each is a function of the
 * subscription's state and never stored.
 */
import {AbsenceReason, type MachineStats} from '@vibesys/proto';
import type {FailureReason, Instant, SubscriptionState} from '@vibesys/subscription';

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

/** Why a host has no stream, in a word. */
export const FAILURE_WORDS: Readonly<Record<FailureReason, string>> = {
  unreachable: 'Unreachable',
  ended: 'Disconnected',
  silent: 'No signal',
  outdated: 'Not updating',
  no_first_frame: 'No reading',
  source_failed: 'Failed',
  resource_exhausted: 'Overloaded',
  internal: 'Failed',
  unsupported: 'Not supported',
  invalid_frame: 'Invalid reading',
  closed: 'Closed',
};

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

function cpuGauge(stats: MachineStats): GaugeView {
  if (stats.cpu.case !== 'cpuStats')
    return {label: 'CPU', share: null, figure: UNREADABLE, tone: 'ok'};
  const used = stats.cpu.value.utilizationPercent;
  return {label: 'CPU', share: used / 100, figure: percent(used), tone: tone(used / 100)};
}

function memoryGauge(stats: MachineStats): GaugeView {
  if (stats.memory.case !== 'memoryStats')
    return {label: 'Memory', share: null, figure: UNREADABLE, tone: 'ok'};
  const used = Number(stats.memory.value.usedBytes);
  const total = Number(stats.memory.value.totalBytes);
  return {
    label: 'Memory',
    share: share(used, total),
    figure: gigabytes(used, total),
    tone: tone(share(used, total)),
  };
}

/** The GPUs as one gauge of their mean use and one of their memory; none for a host without. */
function gpuGauges(stats: MachineStats): readonly GaugeView[] {
  if (stats.gpu.case === 'gpuUnavailable') {
    // A host that has no such tool has no GPU to read; any other absence is a reading missed.
    return stats.gpu.value.reason === AbsenceReason.TOOL_NOT_FOUND
      ? []
      : [{label: 'GPU', share: null, figure: UNREADABLE, tone: 'ok'}];
  }
  const gpus = stats.gpu.case === 'gpus' ? stats.gpu.value.gpus : [];
  if (gpus.length === 0) return [];
  const mean = gpus.reduce((sum, gpu) => sum + gpu.utilizationPercent, 0) / gpus.length;
  const used = gpus.reduce((sum, gpu) => sum + Number(gpu.memoryUsedBytes), 0);
  const total = gpus.reduce((sum, gpu) => sum + Number(gpu.memoryTotalBytes), 0);
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
  return [cpuGauge(stats), memoryGauge(stats), ...gpuGauges(stats)];
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
