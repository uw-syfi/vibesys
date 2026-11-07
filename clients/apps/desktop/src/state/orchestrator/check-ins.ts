/**
 * Catch me up as the app holds it: the check-ins the host stores (`proto/vibesys/orchestrator/v1/checkin.proto`),
 * every viewer's, by id, and the reads a screen needs of them. The host works out and freezes every
 * figure, so nothing here adds, windows or orders figures; this file keeps the latest copy of each
 * check-in and says how far it has come, from the fields the host serves.
 *
 * A check-in only moves forward: pending to ready or failed, and a report unlinked to linked. A copy
 * that is behind the one held is stale and changes nothing (`withCheckIn`), whatever order copies
 * arrive in; an equal one is the later arrival, which is the newer.
 *
 * Reads (`readCheckIn`, `rewind`) are pure functions of the state:
 * - the facts are `pending`, `ready` (frozen) or `failed` (the host's reason), as the host says;
 * - the lead's summary is `writing` while the ask is set, no report is linked and the ask message is
 *   not final (consumed or rejected); `report` once one is linked; and otherwise the reports posted
 *   inside the window, which is all a run that could not be asked has.
 */
import {
  type CheckIn,
  CheckInState,
  Delivery,
  type Report,
  type WindowFacts,
} from '@vibesys/orchestrator-proto';
import {messageIs} from './messages.js';
import type {OrchestratorState} from './state.js';
import {ms} from './time.js';

/** Check-ins by id, as the host sent them. */
export type CheckIns = Readonly<Record<string, CheckIn>>;

/** How far a check-in has come, as one number that only rises: state first, then the linked report. */
function progressOf(checkIn: CheckIn): number {
  const settled = checkIn.state === CheckInState.PENDING ? 0 : 2;
  return settled + (checkIn.reportMessageId === '' ? 0 : 1);
}

/** The snapshot's check-ins, by id. A later copy of an id in one snapshot is the one kept. */
export function checkInsOfSnapshot(checkIns: readonly CheckIn[]): CheckIns {
  return Object.fromEntries(checkIns.map(checkIn => [checkIn.id, checkIn]));
}

/**
 * Stores `checkIn` unless the one held is further along. Every host path only rises, so this never
 * drops a change that came in order, and a stale copy (a pending one after a ready one, a copy
 * without the report after one with it) cannot take a check-in back.
 */
export function withCheckIn(state: OrchestratorState, checkIn: CheckIn): OrchestratorState {
  const held = state.checkIns[checkIn.id];
  if (held !== undefined && progressOf(checkIn) < progressOf(held)) return state;
  return {...state, checkIns: {...state.checkIns, [checkIn.id]: checkIn}};
}

/** Where the lead's summary of a window stands. */
export type Summary =
  /** The lead was asked and has not answered: its request is still on its way. */
  | {readonly kind: 'writing'}
  /** The report the lead posted in answer. */
  | {readonly kind: 'report'; readonly report: Report}
  /** The reports posted inside the window, in the order posted; none when there were none. */
  | {readonly kind: 'reports'; readonly reports: readonly Report[]};

/** What the host says of the facts of a check-in. */
export type Facts =
  | {readonly kind: 'pending'}
  | {readonly kind: 'ready'; readonly facts: WindowFacts}
  | {readonly kind: 'failed'; readonly reason: string};

/** One check-in as a screen reads it. */
export interface CheckInRead {
  readonly checkIn: CheckIn;
  readonly sinceMs: number;
  readonly atMs: number;
  readonly facts: Facts;
  readonly summary: Summary;
}

const isFinal = (delivery: Delivery): boolean =>
  delivery === Delivery.CONSUMED || delivery === Delivery.REJECTED;

function reportsIn(state: OrchestratorState, ids: readonly string[]): Report[] {
  return ids.flatMap(id => {
    const message = state.messages[id];
    return messageIs(message, 'report') ? [message.body.value] : [];
  });
}

function summaryOf(state: OrchestratorState, checkIn: CheckIn): Summary {
  if (checkIn.reportMessageId !== '') {
    const message = state.messages[checkIn.reportMessageId];
    // The report is ordered with its link on the run stream, so a message that is not here is one
    // the snapshot left out: the window's own reports are what is left to show.
    if (messageIs(message, 'report')) return {kind: 'report', report: message.body.value};
  } else if (checkIn.askMessageId !== '') {
    const ask = state.messages[checkIn.askMessageId];
    if (ask === undefined || !isFinal(ask.delivery)) return {kind: 'writing'};
  }
  return {kind: 'reports', reports: reportsIn(state, checkIn.facts?.reportMessageIds ?? [])};
}

function factsOf(checkIn: CheckIn): Facts {
  switch (checkIn.state) {
    case CheckInState.READY:
      return checkIn.facts === undefined
        ? {kind: 'pending'}
        : {kind: 'ready', facts: checkIn.facts};
    case CheckInState.FAILED:
      return {kind: 'failed', reason: checkIn.failure};
    case CheckInState.PENDING:
    case CheckInState.UNSPECIFIED:
      return {kind: 'pending'};
    default:
      checkIn.state satisfies never;
      return {kind: 'pending'};
  }
}

/** `checkIn` with what it says of its window, its facts and its summary. */
export function readCheckIn(state: OrchestratorState, checkIn: CheckIn): CheckInRead {
  return {
    checkIn,
    sinceMs: ms(checkIn.since) ?? 0,
    atMs: ms(checkIn.at) ?? 0,
    facts: factsOf(checkIn),
    summary: summaryOf(state, checkIn),
  };
}

/**
 * The check-ins `viewerId` has made in `runId`, the newest first: by the end of the window, then by
 * its start, then by id, so windows of no length (a press the clock put before the window's start)
 * stay in the order they were made. A failed one is in the list, with its reason.
 */
export function rewind(
  state: OrchestratorState,
  runId: string,
  viewerId: string,
): readonly CheckInRead[] {
  return Object.values(state.checkIns)
    .filter(checkIn => checkIn.runId === runId && checkIn.viewer?.id === viewerId)
    .map(checkIn => readCheckIn(state, checkIn))
    .sort(
      (a, b) =>
        b.atMs - a.atMs || b.sinceMs - a.sinceMs || b.checkIn.id.localeCompare(a.checkIn.id),
    );
}
