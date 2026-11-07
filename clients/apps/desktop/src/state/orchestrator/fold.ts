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
  type Item,
  ItemStatus,
  oneofIs,
  type RunChange,
  type RunsSnapshot,
  type SearchState,
  type Turn,
  TurnStatus,
  type WatchAgentResponse,
} from '@vibesys/orchestrator-proto';
import type {CostView} from './cost.js';
import type {OrchestratorState, Overlay, Progress} from './state.js';

function byId<T extends {readonly id: string}>(rows: readonly T[]): Record<string, T> {
  return Object.fromEntries(rows.map(row => [row.id, row]));
}

/**
 * A read of the runs from scratch: replaces runs, agents and messages wholesale. An older stored
 * sequence leaves those records alone, unless the state is waiting out a gap: the read that was
 * asked for to close the gap is authoritative, even when the daemon's sequence restarted below
 * the one held, or the same gap would reopen forever. What it does not carry (items, receipts,
 * progress) stays, and a turn the agent stream already finished is not taken back to running.
 * Search facts always replace the held map, including omitted runs: their revisions restart
 * with the host, independently of the stored event sequence.
 */
export function applySnapshot(state: OrchestratorState, snapshot: RunsSnapshot): OrchestratorState {
  if (snapshot.sequence < state.sequence && state.gap === null) {
    return {
      ...state,
      searches: Object.fromEntries(snapshot.searches.map(search => [search.runId, search])),
      searchRevisions: Object.fromEntries(
        snapshot.searches.map(search => [search.runId, search.revision]),
      ),
    };
  }
  const turns = snapshot.turns.reduce(withTurn, state).turns;
  const runs = byId(snapshot.runs);
  return {
    ...state,
    sequence: snapshot.sequence,
    searches: Object.fromEntries(snapshot.searches.map(search => [search.runId, search])),
    searchRevisions: Object.fromEntries(
      snapshot.searches.map(search => [search.runId, search.revision]),
    ),
    runs,
    costs: costsOfRuns(state.costs, runs),
    agents: byId(snapshot.agents),
    turns,
    messages: byId(snapshot.messages),
    gap: null,
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

function withItem(state: OrchestratorState, item: Item, sequence: bigint): OrchestratorState {
  const stored = state.items[item.id];
  const at = state.itemSequences[item.id] ?? 0n;
  if (stored !== undefined) {
    if (isFinalItem(stored) && !isFinalItem(item)) return state;
    if (isFinalItem(stored) === isFinalItem(item) && sequence < at) return state;
  }
  const newest = sequence > at ? sequence : at;
  return {
    ...state,
    items: {...state.items, [item.id]: item},
    itemSequences: {...state.itemSequences, [item.id]: newest},
    overlays: overlaysAfter(state.overlays, item, newest),
  };
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
  readonly body: {readonly case: 'older'; readonly value: {readonly items: readonly Item[]}};
  readonly sequence: bigint;
}

/** What the per-agent stream carries, and the older pages read on request. */
export type AgentFrame = Pick<WatchAgentResponse, 'body' | 'sequence'> | OlderFrame;

export function applyAgentFeed(state: OrchestratorState, frame: AgentFrame): OrchestratorState {
  const {body, sequence} = frame;
  switch (body.case) {
    case 'page': {
      // A page reads the agent from scratch, and the progress that follows it is a fresh account.
      const agents = new Set(body.value.items.map(item => item.agentId));
      const overlays = Object.fromEntries(
        Object.entries(state.overlays).filter(([, overlay]) => !agents.has(overlay.agentId)),
      );
      return body.value.items.reduce((next, item) => withItem(next, item, body.value.sequence), {
        ...state,
        overlays,
      });
    }
    case 'older':
      return body.value.items.reduce((next, item) => withItem(next, item, sequence), state);
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
 * A view older than the one held does not: it is current to an earlier log sequence, or to the
 * same one at an earlier instant (the rate of the last hour moves with the host's clock while the
 * log does not). An equal one is the later arrival, which is the newer.
 */
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
