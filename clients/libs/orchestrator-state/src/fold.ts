/**
 * The folds: a snapshot and the events after its sequence become the state, and the per-agent feed
 * (items, progress, turns) lays over it. Plain reducers with no clock and no I/O.
 *
 * Properties the tests hold (`fold.test.ts`):
 * - folding any split of a stream equals folding it whole (the fold keeps everything it needs);
 * - an event at or below the applied sequence changes nothing, so reconnecting from any earlier
 *   sequence gives the same state;
 * - an event past the next one changes nothing but `gap`, so a skipped event is never papered over;
 * - a final turn or item is never replaced by a running one, and progress never outlives the
 *   item update that replaces it.
 */
import {
  type Delta,
  type Event,
  type Item,
  ItemStatus,
  type RunsSnapshot,
  type Turn,
  TurnStatus,
  type WatchAgentResponse,
} from '@vibesys/orchestrator-proto';
import type {OrchestratorState, Overlay} from './state.js';

function byId<T extends {readonly id: string}>(rows: readonly T[]): Record<string, T> {
  return Object.fromEntries(rows.map(row => [row.id, row]));
}

/** A read of the runs from scratch: replaces runs, agents, turns and messages wholesale. */
export function applySnapshot(state: OrchestratorState, snapshot: RunsSnapshot): OrchestratorState {
  return {
    ...state,
    sequence: snapshot.sequence,
    runs: byId(snapshot.runs),
    agents: byId(snapshot.agents),
    turns: byId(snapshot.turns),
    messages: byId(snapshot.messages),
    receipts: {},
    gap: null,
  };
}

function withItem(state: OrchestratorState, item: Item): OrchestratorState {
  const stored = state.items[item.id];
  if (stored !== undefined && isFinalItem(stored) && !isFinalItem(item)) return state;
  const {[item.id]: _dropped, ...overlays} = state.overlays;
  return {...state, items: {...state.items, [item.id]: item}, overlays};
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

/** One committed event. A repeat is ignored; a skipped event records a gap and nothing else. */
export function applyEvent(state: OrchestratorState, event: Event): OrchestratorState {
  if (event.sequence <= state.sequence) return state;
  const expected = state.sequence + 1n;
  if (event.sequence !== expected) return {...state, gap: {expected, got: event.sequence}};
  const next: OrchestratorState = {...state, sequence: event.sequence, gap: null};
  const {body} = event;
  switch (body.case) {
    case 'runUpdated':
      return {...next, runs: {...next.runs, [body.value.id]: body.value}};
    case 'agentUpdated':
      return {...next, agents: {...next.agents, [body.value.id]: body.value}};
    case 'turnUpdated':
      return withTurn(next, body.value);
    case 'itemUpdated':
      return withItem(next, body.value);
    case 'messageUpdated':
      return {...next, messages: {...next.messages, [body.value.id]: body.value}};
    case 'receiptRecorded':
      return {...next, receipts: {...next.receipts, [body.value.commandId]: body.value}};
    default:
      return next;
  }
}

function withDelta(state: OrchestratorState, delta: Delta): OrchestratorState {
  const stored = state.items[delta.itemId];
  if (stored !== undefined && isFinalItem(stored)) return state;
  const before: Overlay = state.overlays[delta.itemId] ?? {
    agentId: delta.agentId,
    text: '',
    commandOutput: '',
  };
  const after: Overlay =
    delta.body.case === 'text'
      ? {...before, text: before.text + delta.body.value}
      : delta.body.case === 'commandOutput'
        ? {...before, commandOutput: before.commandOutput + delta.body.value}
        : before;
  return {...state, overlays: {...state.overlays, [delta.itemId]: after}};
}

/** What the per-agent stream carries, and the older pages read on request. */
export type AgentFeed =
  | WatchAgentResponse['body']
  | {readonly case: 'older'; readonly value: {readonly items: readonly Item[]}};

export function applyAgentFeed(state: OrchestratorState, feed: AgentFeed): OrchestratorState {
  switch (feed.case) {
    case 'page':
    case 'older': {
      let next = state;
      for (const item of feed.value.items) next = withItem(next, item);
      return next;
    }
    case 'item':
      return withItem(state, feed.value);
    case 'delta':
      return withDelta(state, feed.value);
    case 'turn':
      return withTurn(state, feed.value);
    default:
      return state;
  }
}
