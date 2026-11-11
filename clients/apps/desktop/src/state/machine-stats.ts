/**
 * The host's machine stats (CPU, memory and each GPU), folded from the events of the topic's
 * streams. The topic is keyed state that a stream carries from nothing: its entries up to `Synced`
 * are one snapshot, which replaces what is shown only once it is whole, and the changes after it
 * apply in order. A reading is current only while the stream that brought it is open, synced and
 * renewed within its bounds. Those bounds are gaps between receipts (`nextLapse`): the feed
 * (`core/machine-stats-feed.ts`) stamps each frame with its own clock as it arrives and says when a
 * bound passed, so nothing here reads a clock, and nothing compares the time the host sampled at
 * with this machine's.
 */
import type {HostConnectionState} from './connection.js';
import {
  type TopicRefusal as DeliveryRefusal,
  type TopicRetry as DeliveryRetry,
  type TopicEnd,
  topicRetry,
} from './topic-delivery.js';

/** Why the host has no reading of a section (`AbsenceReason`). */
export type AbsenceCause =
  /** The command the section needs is not on the host. */
  | 'no-tool'
  /** The command or file is there and could not be read. */
  | 'read-failed'
  /** The read outlived the sample's deadline. */
  | 'timed-out'
  /** The output was not in the expected format. */
  | 'unparsed'
  /** The tool is there, and nothing backs a parser of its output yet. */
  | 'unverified';

/** A section of a sample: its reading, or why the host has none (never shown as zero). */
export type Section<T> =
  | {readonly _tag: 'Read'; readonly value: T}
  | {readonly _tag: 'Absent'; readonly cause: AbsenceCause};

/** Bytes in use out of a total. */
export interface MemoryUse {
  readonly usedBytes: number;
  readonly totalBytes: number;
}

export interface GpuReading {
  readonly index: number;
  readonly name: string;
  readonly utilizationPercent: number;
  /** On unified memory, the system memory the GPU shares. */
  readonly memory: MemoryUse;
}

/** One reading of the machine the host runs on. */
export interface MachineSample {
  /** The average over every logical CPU since the previous sample. */
  readonly cpuPercent: Section<number>;
  readonly memory: Section<MemoryUse>;
  /** An empty list is the host's word that the machine has no GPU, not a missing reading. */
  readonly gpus: Section<readonly GpuReading[]>;
}

/** What the host refused the topic for (`TopicFailureCode`). */
type TopicRefusal = DeliveryRefusal;

/** How a stream of the topic ended. */
export type MachineEnd = TopicEnd;

/** What happened, in the order the feed saw it. */
export type MachineEvent =
  /** A stream opened: a new snapshot starts. */
  | {readonly _tag: 'Open'}
  /** The key now holds this sample. */
  | {readonly _tag: 'Entry'; readonly key: string; readonly sample: MachineSample}
  /** The key is gone. */
  | {readonly _tag: 'Removed'; readonly key: string}
  /** What the stream sent so far is one whole snapshot. */
  | {readonly _tag: 'Synced'}
  /** The stream is open, and no value came within the topic's bound on a value's age. */
  | {readonly _tag: 'Aged'}
  /** The stream ended. */
  | {readonly _tag: 'End'; readonly why: MachineEnd}
  /** Nobody follows the topic any more: what was read is forgotten. */
  | {readonly _tag: 'Stop'};

export interface MachineStats {
  /** The last whole snapshot and the changes after it, by key; null until a stream first synced. */
  readonly shown: ReadonlyMap<string, MachineSample> | null;
  /** The snapshot the open stream is still reading; null once it synced, or with no stream open. */
  readonly reading: ReadonlyMap<string, MachineSample> | null;
  /** A stream is open. */
  readonly open: boolean;
  /** The open stream's values passed their age bound, and none came since. */
  readonly aged: boolean;
  /** How the last stream ended; kept until a new one syncs, so a reopen still says why. */
  readonly ended: MachineEnd | null;
}

export const emptyMachineStats: MachineStats = {
  shown: null,
  reading: null,
  open: false,
  aged: false,
  ended: null,
};

/** The key set `keys` with `event` applied, or null when it breaks the bound on keys. */
function changed(
  keys: ReadonlyMap<string, MachineSample>,
  event: Extract<MachineEvent, {readonly _tag: 'Entry' | 'Removed'}>,
  maxKeys: number,
): ReadonlyMap<string, MachineSample> | null {
  const next = new Map(keys);
  if (event._tag === 'Entry') next.set(event.key, event.sample);
  else next.delete(event.key);
  return next.size > maxKeys ? null : next;
}

/** An entry or a removal on the open stream: into the snapshot it reads, or onto what is shown. */
function withChange(
  model: MachineStats,
  event: Extract<MachineEvent, {readonly _tag: 'Entry' | 'Removed'}>,
  maxKeys: number,
): MachineStats {
  const keys = changed(model.reading ?? model.shown ?? new Map(), event, maxKeys);
  if (keys === null) {
    const detail = `more than ${maxKeys} ${maxKeys === 1 ? 'key' : 'keys'}`;
    return {...model, reading: null, open: false, ended: {_tag: 'Invalid', detail}};
  }
  if (model.reading !== null) return {...model, reading: keys};
  // A value renews what is shown; a removal is a change of the state, not a fresh value.
  return {...model, shown: keys, aged: event._tag === 'Entry' ? false : model.aged};
}

/**
 * `model` after `event`. A frame of a stream that is no longer open changes nothing. A stream
 * that brings more keys than `maxKeys` (the topic's bound) broke its contract: it is ended as
 * invalid, so the feed stops reading it (`open` turns false), and what is shown is kept.
 */
export function applyMachineEvent(
  model: MachineStats,
  event: MachineEvent,
  maxKeys: number,
): MachineStats {
  switch (event._tag) {
    case 'Open':
      return {...model, reading: new Map(), open: true, aged: false};
    case 'Stop':
      return emptyMachineStats;
    case 'End':
      return model.open ? {...model, reading: null, open: false, ended: event.why} : model;
    case 'Entry':
    case 'Removed':
      return model.open ? withChange(model, event, maxKeys) : model;
    case 'Synced':
      return model.open && model.reading !== null
        ? {...model, shown: model.reading, reading: null, aged: false, ended: null}
        : model;
    case 'Aged':
      return model.open && model.reading === null ? {...model, aged: true} : model;
  }
}

/**
 * What the feed does after a stream ended: open the next one after a backoff; hand the connection
 * a lost link and open the next one on the new link; or wait, without blaming the link, until the
 * link is new anyway.
 */
export type MachineRetry = DeliveryRetry;

/**
 * Which ends a new stream may cure soon. A refusal that no retry helps, a contract the host broke
 * and a call it refused stay until the link to the host is made again (a host may have been
 * upgraded meanwhile); a lost link is the connection's to redial.
 */
export function machineRetry(end: MachineEnd): MachineRetry {
  return topicRetry(end);
}

/** The bounds `subscribe.proto` holds a stream of the topic to, in milliseconds. */
export interface MachineBounds {
  /** No frame of any kind for this long: the stream is lost, whatever the link says. */
  readonly leaseMs: number;
  /** While the snapshot is read, no entry, removal or `Synced` this long: the topic is silent. */
  readonly firstFrameMs: number;
  /** After the snapshot, no value for this long: what is shown is not current. */
  readonly staleAfterMs: number;
}

/**
 * When the open stream last brought each kind of frame, in milliseconds on the feed's own clock (a
 * stream's opening counts as each). Only gaps between these are ever measured.
 */
export interface Receipts {
  /** Any frame, a heartbeat included: the stream is alive. */
  readonly frame: number;
  /** An entry, a removal or `Synced`: the topic says something. */
  readonly data: number;
  /** An entry: a new value. */
  readonly value: number;
}

/** What a frame renews: the stream only, the topic, or the value too. */
export type Receipt = 'heartbeat' | 'data' | 'value';

export const openedAt = (at: number): Receipts => ({frame: at, data: at, value: at});

export function received(receipts: Receipts, receipt: Receipt, at: number): Receipts {
  return {
    frame: at,
    data: receipt === 'heartbeat' ? receipts.data : at,
    value: receipt === 'value' ? at : receipts.value,
  };
}

/** Which bound passed: the lease and the snapshot's wait end a stream, a value's age marks it. */
export type Lapse = 'lease' | 'snapshot' | 'value';

/**
 * The first bound the open stream passes if nothing more arrives: when, and which. The lease holds
 * always; the wait for the snapshot until it is whole; the value's age after it, until it passed.
 * On a tie the bound that ends the stream wins.
 */
export function nextLapse(
  model: MachineStats,
  receipts: Receipts,
  bounds: MachineBounds,
): {readonly at: number; readonly lapse: Lapse} {
  const lease = {at: receipts.frame + bounds.leaseMs, lapse: 'lease'} as const;
  if (model.reading !== null) {
    const snapshot = receipts.data + bounds.firstFrameMs;
    return snapshot < lease.at ? {at: snapshot, lapse: 'snapshot'} : lease;
  }
  if (model.aged || bounds.staleAfterMs === 0) return lease;
  const value = receipts.value + bounds.staleAfterMs;
  return value < lease.at ? {at: value, lapse: 'value'} : lease;
}

/** Why the screen cannot show a current reading. */
export type MachineWhy =
  /** The link to the host is down. */
  | 'disconnected'
  /** A stream is opening, or reading its snapshot. */
  | 'updating'
  /** The stream is open, and its value passed its age bound. */
  | 'not-updating'
  /** The stream said nothing within its bound. */
  | 'silent'
  /** The host has no reading to give (its snapshot holds none). */
  | 'no-readings'
  /** The host cannot sample its machine, or predates the topic. */
  | 'unsupported'
  | 'over-limit'
  | 'read-failed'
  /** What the host sent broke the contract. */
  | 'bad-readings'
  /** The host failed the stream, or refused the call. */
  | 'failed';

/** What the screen shows of the host's machine. */
export type MachineView =
  /** Nothing read yet, and nothing wrong yet. */
  | {readonly _tag: 'Waiting'}
  /** A reading; `stale` says why it is not current, null while it is. */
  | {readonly _tag: 'Shown'; readonly sample: MachineSample; readonly stale: MachineWhy | null}
  /** No reading, and why. */
  | {readonly _tag: 'Missing'; readonly why: MachineWhy};

const REFUSAL_WHY: Readonly<Record<TopicRefusal, MachineWhy>> = {
  unsupported: 'unsupported',
  'over-limit': 'over-limit',
  'source-failed': 'read-failed',
  internal: 'failed',
};

function endWhy(end: MachineEnd): MachineWhy {
  switch (end._tag) {
    case 'Refused':
      return REFUSAL_WHY[end.refusal];
    case 'Silent':
      return 'silent';
    case 'Closed':
      return 'not-updating';
    case 'Invalid':
      return 'bad-readings';
    case 'Failed':
      if (end.fault.kind === 'Unimplemented') return 'unsupported';
      if (end.fault.kind === 'InvalidMessage') return 'bad-readings';
      // A reader that fell behind opens a new stream after a short wait.
      if (end.fault.kind === 'FellBehind') return 'updating';
      return topicRetry(end) === 'lost' ? 'disconnected' : 'failed';
  }
}

function whyNotCurrent(connection: HostConnectionState, model: MachineStats): MachineWhy | null {
  const ended = model.ended === null ? null : endWhy(model.ended);
  // After a lost link the next stream opens only on a new link, so an open one says the loss is
  // over; whether the link is up now is the connection's to say.
  if (ended !== null && !(ended === 'disconnected' && model.open)) return ended;
  if (connection._tag !== 'Ready') return 'disconnected';
  if (!model.open || model.reading !== null) return 'updating';
  return model.aged ? 'not-updating' : null;
}

const WAITING: MachineView = {_tag: 'Waiting'};

/**
 * What the screen shows of the host's machine, given its connection. The topic holds one key, so
 * the reading is the first one shown. A first dial or a first snapshot under way is a wait, not
 * a fault; any reading that is not current says why.
 */
export function machineView(connection: HostConnectionState, model: MachineStats): MachineView {
  const why = whyNotCurrent(connection, model);
  if (model.shown === null) {
    const dialing = connection._tag === 'Idle' || connection._tag === 'Connecting';
    return why === null || why === 'updating' || (why === 'disconnected' && dialing)
      ? WAITING
      : {_tag: 'Missing', why};
  }
  const [sample] = model.shown.values();
  if (sample === undefined) {
    return {_tag: 'Missing', why: why === null || why === 'updating' ? 'no-readings' : why};
  }
  return {_tag: 'Shown', sample, stale: why};
}
