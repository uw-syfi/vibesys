/**
 * What the Machines page shows of a host's live stats: the subscription's state as a few gauges,
 * a stale value or a status word. Labels, numbers and status words only; each is a function of the
 * subscription's state and never stored.
 */
import {
  type FailureReason,
  type Instant,
  type MachineReadings,
  type MachineStats,
  machineReadings,
  type SubscriptionState,
} from '@vibesys/subscription';
import type {StatusTone} from './words.js';

/** A reading is drawn in the warn tone from here: the share of its capacity in use. */
const HOT_SHARE = 0.9;

/** One reading of a gauge: its label, how full it is (null: no reading), and its figure. */
export interface GaugeView {
  readonly label: 'CPU' | 'Memory' | 'GPU' | 'VRAM';
  readonly share: number | null;
  readonly figure: string;
  /** The characters of the widest figure this gauge shows, so the row never moves as it changes. */
  readonly width: number;
  readonly tone: 'ok' | 'warn';
}

export type StatsView =
  /** No value yet, and the stream is on its way. */
  | {readonly kind: 'waiting'}
  /**
   * Values, as of when the last one arrived. `stale` is set when the host stopped delivering, and
   * `reconnecting` when a stream is being opened right now (after Retry or a wake), not waited for.
   */
  | {
      readonly kind: 'values';
      readonly gauges: readonly GaugeView[];
      readonly stale: {readonly valueAt: Instant; readonly reconnecting: boolean} | null;
    }
  /** No value and no stream: why, and whether a press can help. */
  | {readonly kind: 'failed'; readonly reason: FailureReason; readonly retry: boolean};

/** What a card's stats row needs of one host: what to draw, how old it is, and the press to retry. */
export interface HostLive {
  readonly view: StatsView;
  /** How old a stale value is, as words (`42 s`); null while it is current, or before the clock reads. */
  readonly age: string | null;
  readonly retry: () => void;
}

const UNREADABLE = '–';
const GIB = 2 ** 30;
/** The room of `100%`, with a character to spare for a face whose `%` is wide. */
const PERCENT_WIDTH = 5;

/** Why a host has no stream, in a word or two, of its readings and not of the machine. */
export const FAILURE_WORDS: Readonly<Record<FailureReason, string>> = {
  unreachable: 'Unreachable',
  ended: 'Disconnected',
  silent: 'Silent',
  outdated: 'Not updating',
  no_first_frame: 'No readings',
  source_failed: 'Read failed',
  resource_exhausted: 'Over limit',
  internal: 'Read failed',
  unsupported: 'Not supported',
  invalid_frame: 'Bad readings',
  closed: 'Closed',
};

/** How each failure is drawn: a host that cannot give readings is an error unless it never could. */
export const FAILURE_TONES: Readonly<Record<FailureReason, StatusTone>> = {
  unreachable: 'err',
  ended: 'err',
  silent: 'err',
  outdated: 'err',
  no_first_frame: 'err',
  source_failed: 'err',
  resource_exhausted: 'err',
  internal: 'err',
  unsupported: 'warn',
  invalid_frame: 'err',
  closed: 'neutral',
};

function tone(share: number): GaugeView['tone'] {
  return share >= HOT_SHARE ? 'warn' : 'ok';
}

function percent(value: number): string {
  return `${Math.round(value)}%`;
}

/** `used / total GB`, with a decimal while the total is small enough for one to matter. */
function gigabytes(used: number, total: number): string {
  const digits = total >= 100 * GIB ? 0 : 1;
  return `${(used / GIB).toFixed(digits)} / ${(total / GIB).toFixed(digits)} GB`;
}

/** The room of the widest `used / total GB` for this total: the figure when `used` is `total`. */
function gigabytesWidth(total: number): number {
  return gigabytes(total, total).length + 1;
}

function share(used: number, total: number): number {
  return total > 0 ? Math.min(1, used / total) : 0;
}

/** A gauge for a reading that was not made: empty, and `–` in the room the figure would take. */
function unread(label: GaugeView['label'], width: number): GaugeView {
  return {label, share: null, figure: UNREADABLE, width, tone: 'ok'};
}

function cpuGauge({cpuPercent}: MachineReadings): GaugeView {
  if (cpuPercent === null) return unread('CPU', PERCENT_WIDTH);
  const full = cpuPercent / 100;
  return {
    label: 'CPU',
    share: full,
    figure: percent(cpuPercent),
    width: PERCENT_WIDTH,
    tone: tone(full),
  };
}

function memoryGauge({memory}: MachineReadings): GaugeView {
  // Unread, the room is a typical total's, so the row is as wide as it will be once read.
  if (memory === null) return unread('Memory', gigabytesWidth(512 * GIB));
  const full = share(memory.usedBytes, memory.totalBytes);
  return {
    label: 'Memory',
    share: full,
    figure: gigabytes(memory.usedBytes, memory.totalBytes),
    width: gigabytesWidth(memory.totalBytes),
    tone: tone(full),
  };
}

/** The GPUs as one gauge of their mean use and one of their memory; none for a host without. */
function gpuGauges({gpus: reading}: MachineReadings): readonly GaugeView[] {
  // A host with no such tool has no GPU to read; any other absence is a reading missed.
  if (reading.kind === 'no-tool') return [];
  if (reading.kind === 'unreadable')
    return [unread('GPU', PERCENT_WIDTH), unread('VRAM', gigabytesWidth(512 * GIB))];
  const {gpus} = reading;
  if (gpus.length === 0) return [];
  const mean = gpus.reduce((sum, gpu) => sum + gpu.utilizationPercent, 0) / gpus.length;
  const used = gpus.reduce((sum, gpu) => sum + gpu.usedBytes, 0);
  const total = gpus.reduce((sum, gpu) => sum + gpu.totalBytes, 0);
  return [
    {
      label: 'GPU',
      share: mean / 100,
      figure: percent(mean),
      width: PERCENT_WIDTH,
      tone: tone(mean / 100),
    },
    {
      label: 'VRAM',
      share: share(used, total),
      figure: gigabytes(used, total),
      width: gigabytesWidth(total),
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
      return {kind: 'waiting'};
    case 'live':
      return {kind: 'values', gauges: statsGauges(state.value), stale: null};
    case 'stale':
      return {
        kind: 'values',
        gauges: statsGauges(state.value),
        stale: {valueAt: state.valueAt, reconnecting: state.attempt === 0},
      };
    case 'failed':
      return {kind: 'failed', reason: state.reason, retry: state.reason !== 'closed'};
  }
}

/** What the stream says of a host, as a status: one word and its tone. */
export function streamStatus(view: StatsView): {readonly word: string; readonly tone: StatusTone} {
  switch (view.kind) {
    case 'waiting':
      return {word: 'Connecting', tone: 'neutral'};
    case 'values':
      if (view.stale === null) return {word: 'Online', tone: 'ok'};
      return view.stale.reconnecting
        ? {word: 'Connecting', tone: 'neutral'}
        : {word: 'Stale', tone: 'warn'};
    case 'failed':
      return {word: FAILURE_WORDS[view.reason], tone: FAILURE_TONES[view.reason]};
  }
}

/** How old a value is, in the largest whole unit: `42 s`, `3 min`, `2 h`. Never negative. */
export function ageWord(now: Instant, valueAt: Instant): string {
  const seconds = Math.max(0, Math.floor((now - valueAt) / 1000));
  if (seconds < 60) return `${seconds} s`;
  const minutes = Math.floor(seconds / 60);
  return minutes < 60 ? `${minutes} min` : `${Math.floor(minutes / 60)} h`;
}
