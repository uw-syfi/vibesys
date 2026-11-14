/**
 * The folds: a snapshot and the changes after its sequence become the state, and the per-agent feed
 * (items, progress, turns) lays over it. Plain reducers with no clock and no I/O.
 *
 * Two of the three streams carry whole items (the run stream's `item`, the agent stream's page and
 * item frames; the third carries a run's cost), and neither item has a revision of its own. The revision is the committed `sequence`
 * the item was carried at: the change's, or the agent frame's. Which whole item wins:
 * 1. a final item (finished, failed) is never replaced by a running one, whatever its sequence;
 * 2. between two of the same finality, the higher sequence wins, and an equal one is the later
 *    arrival (one agent stream is in order, so its later frame is its newer one).
 * A delta made at sequence `s` extends the item stored at sequence `t` only when `s >= t`; a lower
 * `s` means the stored item was committed after the progress and already holds it.
 *
 * Properties the tests hold (`fold.properties.test.ts`):
 * - changes in any order, repeated any number of times, leave the state of the longest gap-free
 *   prefix received, so a skipped change is never papered over and a repeat changes nothing;
 * - a snapshot over a populated state keeps what the snapshot does not carry (items, receipts,
 *   progress) and takes the state back to an earlier sequence only to close a gap;
 * - however the run stream and the agent stream interleave, every item ends as the newest copy
 *   either stream carried, and progress shown is never text a stored item already holds.
 */
import {
  type Delta,
  type Elision,
  type GetRunHistoryResponse,
  type Item,
  ItemStatus,
  oneofIs,
  type RunChange,
  type RunSummary,
  type RunsSnapshot,
  type SearchState,
  type Turn,
  TurnStatus,
  type WatchAgentResponse,
} from '@vibesys/orchestrator-proto';
import {checkInsOfSnapshot, withCheckIn} from './check-ins.js';
import type {CostView} from './cost.js';
import type {HostCostView, HostCostWindow} from './host-cost.js';
import type {OrchestratorState, Overlay, Progress} from './state.js';
import {ms} from './time.js';

function byId<T extends {readonly id: string}>(rows: readonly T[]): Record<string, T> {
  return Object.fromEntries(rows.map(row => [row.id, row]));
}

const summariesOf = (summaries: readonly RunSummary[]): OrchestratorState['summaries'] =>
  Object.fromEntries(summaries.map(summary => [summary.runId, summary]));

/**
 * A read of the runs from scratch: replaces the runs, their summaries, what the host says it left out
 * and every history it carries (agents, turns, messages, check-ins: a snapshot of detail SUMMARY, which
 * is what the app asks for, carries none, so none is held after it, and a screen of a run reads the
 * history again with `GetRunHistory`). A snapshot is read because the stream's continuity was lost, so
 * a record held from before it cannot be told from a stale one. An older stored sequence leaves the
 * runs alone, unless the state is waiting out a gap: the read that was asked for to close the gap is
 * authoritative, even when the daemon's sequence restarted below the one held, or the same gap would
 * reopen forever. What it does not carry (items, receipts, progress) stays, and a turn the agent stream
 * already finished is not taken back to running.
 * Search facts and summaries always replace the held maps, including omitted runs: their revisions
 * restart with the host, independently of the stored event sequence.
 */
function snapshotState(
  state: OrchestratorState,
  snapshot: RunsSnapshot,
  replace: boolean,
): OrchestratorState {
  const searches = Object.fromEntries(snapshot.searches.map(search => [search.runId, search]));
  const searchRevisions = Object.fromEntries(
    snapshot.searches.map(search => [search.runId, search.revision]),
  );
  const cut = {truncated: snapshot.truncated, runsLeftOut: snapshot.runsLeftOut};
  const models = snapshot.models ?? null;
  if (!replace && snapshot.sequence < state.sequence && state.gap === null) {
    return {
      ...state,
      searches,
      searchRevisions,
      summaries: summariesOf(snapshot.summaries),
      cut,
      models,
    };
  }
  const turns = snapshot.turns.reduce(withTurn, state).turns;
  const runs = byId(snapshot.runs);
  return {
    ...state,
    sequence: snapshot.sequence,
    searches,
    searchRevisions,
    runs,
    summaries: summariesOf(snapshot.summaries),
    cut,
    models,
    histories: {},
    costs: costsOfRuns(state.costs, runs),
    agents: byId(snapshot.agents),
    turns,
    messages: byId(snapshot.messages),
    checkIns: checkInsOfSnapshot(snapshot.checkIns),
    gap: null,
  };
}

export function applySnapshot(state: OrchestratorState, snapshot: RunsSnapshot): OrchestratorState {
  return snapshotState(state, snapshot, false);
}

/**
 * Replace the run stream's facts with a snapshot explicitly read from sequence zero. Unlike a
 * snapshot that arrived while resuming a stream, this read is authoritative even when the host
 * restarted at a lower sequence. The replacement is one fold, so callers retain the old state
 * until the snapshot has arrived and never expose an invented gap between the two.
 */
export function replaceSnapshot(
  state: OrchestratorState,
  snapshot: RunsSnapshot,
): OrchestratorState {
  return snapshotState(state, snapshot, true);
}

/**
 * One whole summary replaces the one held for its run: the host sends whole summaries, never parts, and
 * the latest to arrive on the one stream is the newest.
 */
export function applySummary(state: OrchestratorState, summary: RunSummary): OrchestratorState {
  return {...state, summaries: {...state.summaries, [summary.runId]: summary}};
}

/**
 * One page of a run's history, read on request (`GetRunHistory`), with the run it is of and where it
 * stands among the run's pages.
 */
export interface RunHistoryFrame {
  readonly runId: string;
  readonly page: Pick<
    GetRunHistoryResponse,
    | 'sequence'
    | 'agents'
    | 'turns'
    | 'checkIns'
    | 'messages'
    | 'turnsLeftOutCount'
    | 'checkInsLeftOutCount'
    | 'agentsLeftOutCount'
  >;
  /** This is the run's first page, which holds what is not paged, so it says what the host left out. */
  readonly first: boolean;
  /** This is the last page: the run's history is whole after it. */
  readonly last: boolean;
}

/** The records of two copies of a set: where `authoritative`, `incoming` wins a record held in both. */
function merged<T>(
  held: Readonly<Record<string, T>>,
  incoming: Readonly<Record<string, T>>,
  authoritative: boolean,
): Record<string, T> {
  return authoritative ? {...held, ...incoming} : {...incoming, ...held};
}

/**
 * Folds one page of a run's history. The page is current to its own sequence and the stream goes on
 * delivering what happens after it. A record the state does not hold is added. One it holds and the
 * page carries is the newer of the two when the page is at or past the sequence the state has applied,
 * and then the page replaces it; when the state has applied events past the page, the held record is
 * the newer and stays. Either way the stream's changes bring a record to its newest, so the history
 * and the stream agree whichever arrives first. A turn the stream already finished is not taken back
 * to running (`withTurn`), and a check-in only moves forward (`withCheckIn`).
 */
export function applyRunHistory(
  state: OrchestratorState,
  frame: RunHistoryFrame,
): OrchestratorState {
  const {page, runId} = frame;
  // A snapshot between two pages forgot the first, so the pages that follow are of a history that is no
  // longer being read: the reader starts again from the first page.
  if (!frame.first && state.histories[runId] === undefined) return state;
  const authoritative = page.sequence >= state.sequence;
  const turns = page.turns.reduce(withTurn, state).turns;
  const checkIns = page.checkIns.reduce(withCheckIn, state).checkIns;
  // What the host left out is said on the first page, and the pages after it say nothing of it.
  const before = frame.first ? undefined : state.histories[runId];
  const read = {
    sequence: before?.sequence ?? page.sequence,
    turnsLeftOut: before?.turnsLeftOut ?? page.turnsLeftOutCount,
    checkInsLeftOut: before?.checkInsLeftOut ?? page.checkInsLeftOutCount,
    agentsLeftOut: before?.agentsLeftOut ?? page.agentsLeftOutCount,
    complete: frame.last,
  };
  return {
    ...state,
    agents: merged(state.agents, byId(page.agents), authoritative),
    turns,
    messages: merged(state.messages, byId(page.messages), authoritative),
    checkIns,
    histories: {...state.histories, [runId]: read},
  };
}

const sumOf = (pieces: readonly Progress[], key: 'text' | 'commandOutput'): string =>
  pieces.map(piece => piece[key]).join('');

function overlayOf(agentId: string, pieces: readonly Progress[]): Overlay {
  return {
    agentId,
    text: sumOf(pieces, 'text'),
    commandOutput: sumOf(pieces, 'commandOutput'),
    pieces,
  };
}

/** The progress that an item stored at `sequence` does not hold yet (none, once it is final). */
function overlaysAfter(
  overlays: OrchestratorState['overlays'],
  item: Item,
  sequence: bigint,
): OrchestratorState['overlays'] {
  const {[item.id]: overlay, ...rest} = overlays;
  if (overlay === undefined || isFinalItem(item)) return rest;
  const pieces = overlay.pieces.filter(piece => piece.sequence >= sequence);
  return pieces.length === 0 ? rest : {...rest, [item.id]: overlayOf(overlay.agentId, pieces)};
}

/**
 * Stores `item` when it is the newer copy. `elided` is how many bytes of text the copy left out (a
 * page does that for large reasoning and tool output); null for a whole item. A final item does not
 * change, so a whole copy beats an elided one whatever the sequences say, and an elided copy never
 * replaces a whole one.
 */
/** Whether a copy of an item (elided or whole, at `sequence`) takes the place of the one stored. */
function replaces(
  state: OrchestratorState,
  stored: Item,
  item: Item,
  sequence: bigint,
  elided: number | null,
): boolean {
  if (isFinalItem(stored) && !isFinalItem(item)) return false;
  if (isFinalItem(stored) !== isFinalItem(item)) return true;
  const heldElided = state.elided[item.id] !== undefined;
  // A final item does not change: a whole copy beats an elided one, whatever the sequences say.
  if (isFinalItem(item) && !heldElided && elided !== null) return false;
  if (isFinalItem(item) && heldElided && elided === null) return true;
  return sequence >= (state.itemSequences[item.id] ?? 0n);
}

/**
 * Stores `item` when it is the newer copy. `elided` is how many bytes of text the copy left out (a
 * page does that for large reasoning and tool output); null for a whole item. An elided copy never
 * replaces a whole final one (see `replaces`).
 */
function withItem(
  state: OrchestratorState,
  item: Item,
  sequence: bigint,
  elided: number | null = null,
): OrchestratorState {
  const stored = state.items[item.id];
  if (stored !== undefined && !replaces(state, stored, item, sequence, elided)) return state;
  const newest =
    sequence > (state.itemSequences[item.id] ?? 0n) ? sequence : state.itemSequences[item.id];
  const {[item.id]: _was, ...rest} = state.elided;
  return {
    ...state,
    items: {...state.items, [item.id]: item},
    itemSequences: {...state.itemSequences, [item.id]: newest ?? sequence},
    elided: elided === null ? rest : {...rest, [item.id]: elided},
    overlays: overlaysAfter(state.overlays, item, newest ?? sequence),
  };
}

/** The items of a page with what the page says it left out of each, by ordinal. */
function withPage(
  state: OrchestratorState,
  items: readonly Item[],
  elisions: readonly Elision[],
  sequence: bigint,
): OrchestratorState {
  const left = new Map(elisions.map(elision => [elision.ordinal, Number(elision.bodyBytes)]));
  return items.reduce(
    (next, item) => withItem(next, item, sequence, left.get(item.ordinal) ?? null),
    state,
  );
}

function withTurn(state: OrchestratorState, turn: Turn): OrchestratorState {
  const stored = state.turns[turn.id];
  if (
    stored !== undefined &&
    stored.status !== TurnStatus.RUNNING &&
    turn.status === TurnStatus.RUNNING
  ) {
    return state;
  }
  return {...state, turns: {...state.turns, [turn.id]: turn}};
}

export function isFinalItem(item: Item): boolean {
  return item.status === ItemStatus.FINISHED || item.status === ItemStatus.FAILED;
}

/** One committed change. A repeat is ignored; a skipped one records a gap and nothing else. */
export function applyChange(state: OrchestratorState, change: RunChange): OrchestratorState {
  if (change.sequence <= state.sequence) return state;
  const expected = state.sequence + 1n;
  if (change.sequence !== expected) return {...state, gap: {expected, got: change.sequence}};
  const next: OrchestratorState = {...state, sequence: change.sequence, gap: null};
  const {body} = change;
  switch (body.case) {
    case 'run':
      return {...next, runs: {...next.runs, [body.value.id]: body.value}};
    case 'agent':
      return {...next, agents: {...next.agents, [body.value.id]: body.value}};
    case 'turn':
      return withTurn(next, body.value);
    case 'item':
      return withItem(next, body.value, change.sequence);
    case 'message':
      return {...next, messages: {...next.messages, [body.value.id]: body.value}};
    case 'receipt':
      return {...next, receipts: {...next.receipts, [body.value.commandId]: body.value}};
    case 'checkIn':
      return withCheckIn(next, body.value);
    case 'runUpdated': {
      // The run's own change carries the new settings; this one is the timeline's entry. The change's
      // sequence is strictly higher than any held, so appending keeps the list in order.
      const runId = body.value.runId;
      const record = {sequence: change.sequence, atMs: ms(change.at) ?? 0, update: body.value};
      return {
        ...next,
        settingChanges: {
          ...next.settingChanges,
          [runId]: [...(next.settingChanges[runId] ?? []), record],
        },
      };
    }
    case undefined:
      return next;
    default:
      body satisfies never;
      return next;
  }
}

function withDelta(state: OrchestratorState, delta: Delta, sequence: bigint): OrchestratorState {
  const stored = state.items[delta.itemId];
  if (stored !== undefined && isFinalItem(stored)) return state;
  // The stored item was committed after this progress was made, so it already holds it.
  if (sequence < (state.itemSequences[delta.itemId] ?? 0n)) return state;
  const piece: Progress = {
    sequence,
    text: oneofIs(delta.body, 'text', {text: true, commandOutput: true}) ? delta.body.value : '',
    commandOutput: oneofIs(delta.body, 'commandOutput', {text: true, commandOutput: true})
      ? delta.body.value
      : '',
  };
  const before = state.overlays[delta.itemId];
  const pieces = [...(before?.pieces ?? []), piece];
  return {
    ...state,
    overlays: {
      ...state.overlays,
      [delta.itemId]: overlayOf(before?.agentId ?? delta.agentId, pieces),
    },
  };
}

/** A page of items read on request (`ListItems`): older history, with the sequence it is current to. */
export interface OlderFrame {
  readonly body: {
    readonly case: 'older';
    readonly value: {readonly items: readonly Item[]; readonly elisions: readonly Elision[]};
  };
  readonly sequence: bigint;
}

/** One item read whole on request (`GetItem`), with the sequence it is current to. */
interface WholeFrame {
  readonly body: {readonly case: 'whole'; readonly value: {readonly item: Item}};
  readonly sequence: bigint;
}

/** What the per-agent stream carries, and what is read on request. */
export type AgentFrame = Pick<WatchAgentResponse, 'body' | 'sequence'> | OlderFrame | WholeFrame;

export function applyAgentFeed(state: OrchestratorState, frame: AgentFrame): OrchestratorState {
  const {body, sequence} = frame;
  switch (body.case) {
    case 'page': {
      // A page reads the agent from scratch, and the progress that follows it is a fresh account.
      const agents = new Set(body.value.items.map(item => item.agentId));
      const overlays = Object.fromEntries(
        Object.entries(state.overlays).filter(([, overlay]) => !agents.has(overlay.agentId)),
      );
      return withPage(
        {...state, overlays},
        body.value.items,
        body.value.elisions,
        body.value.sequence,
      );
    }
    case 'older':
      return withPage(state, body.value.items, body.value.elisions, sequence);
    case 'whole':
      return withItem(state, body.value.item, sequence);
    case 'item':
      return withItem(state, body.value, sequence);
    case 'delta':
      return withDelta(state, body.value, sequence);
    case 'turn':
      return withTurn(state, body.value);
    case undefined:
      return state;
    default:
      body satisfies never;
      return state;
  }
}

/** Whole search revisions are comparable only within the current WatchRuns stream. */
export function applySearch(state: OrchestratorState, search: SearchState): OrchestratorState {
  const held = state.searchRevisions[search.runId];
  if (held !== undefined && search.revision <= held) return state;
  return {
    ...state,
    searches: {...state.searches, [search.runId]: search},
    searchRevisions: {...state.searchRevisions, [search.runId]: search.revision},
  };
}

/** The cost views of the runs the list still has: a run the host dropped takes its view with it. */
function costsOfRuns(
  costs: OrchestratorState['costs'],
  runs: OrchestratorState['runs'],
): OrchestratorState['costs'] {
  return Object.fromEntries(Object.entries(costs).filter(([runId]) => runId in runs));
}

/**
 * One whole cost view replaces the one held for its run: the host sends whole views, never parts.
 * The initial view of a new stream is authoritative even when the host restarted at an earlier
 * sequence. Later views older than one already held in that stream do not replace it: they are
 * current to an earlier log sequence, or to the same one at an earlier instant (the rate of the
 * last hour moves with the host's clock while the log does not). An equal one is the later arrival,
 * which is the newer.
 */
export type WholeViewPhase = 'initial' | 'update';

/** The authoritative initial view of a new run-cost stream. */
export function replaceCost(state: OrchestratorState, view: CostView): OrchestratorState {
  return {...state, costs: {...state.costs, [view.runId]: view}};
}

export function applyCost(state: OrchestratorState, view: CostView): OrchestratorState {
  const held = state.costs[view.runId];
  if (
    held !== undefined &&
    (view.sequence < held.sequence ||
      (view.sequence === held.sequence && view.asOfMs < held.asOfMs))
  ) {
    return state;
  }
  return {...state, costs: {...state.costs, [view.runId]: view}};
}

/**
 * One whole host cost view replaces the one held for its window: the host sends whole views, never
 * parts. As with a run's, a new stream's initial view is authoritative; a later view older than the
 * one held does not replace it, and an equal one is the later arrival, which is the newer. A window
 * is held apart from every other.
 */
export function applyHostCost(
  state: OrchestratorState,
  window: HostCostWindow,
  view: HostCostView,
): OrchestratorState {
  const held = state.hostCosts[window];
  if (
    held !== undefined &&
    (view.sequence < held.sequence ||
      (view.sequence === held.sequence && view.asOfMs < held.asOfMs))
  ) {
    return state;
  }
  return {...state, hostCosts: {...state.hostCosts, [window]: view}};
}

/** The authoritative initial view of a new host-cost stream. */
export function replaceHostCost(
  state: OrchestratorState,
  window: HostCostWindow,
  view: HostCostView,
): OrchestratorState {
  return {...state, hostCosts: {...state.hostCosts, [window]: view}};
}
