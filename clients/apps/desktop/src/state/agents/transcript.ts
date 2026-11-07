/**
 * One agent's session as a conversation, top to bottom: for each turn a divider, the message that
 * started it, then the agent's thinking, words and tool calls in order, with a row where it
 * spawned an agent and where one joined back. A pure function of the state and `nowMs` (no clock
 * here); the screens turn each entry into words.
 *
 * Properties the tests hold (`transcript.properties.test.ts`), for any items, turns, messages and
 * children, loaded in any amount:
 * - every loaded item the transcript shows appears exactly once, in item order, and a prompt item
 *   shows only where no message started its turn;
 * - the turns shown are in order, each divider is followed by that turn's entries only, and a turn
 *   with no loaded items is shown only when it is newer than every loaded one;
 * - a spawn or join row appears once per child and per result, after the divider of the last turn
 *   that began before it (the first turn, when none had), and never between the entries of a turn
 *   out of time order;
 * - nothing is shown for a thought or a said text with no words, and a thought has no span when the
 *   host did not say when it started.
 */
import {
  type Agent,
  AgentStatus,
  type Item,
  ItemStatus,
  type Message,
  type ToolCall,
  type Turn,
  TurnStatus,
} from '@vibesys/orchestrator-proto';
import type {OrchestratorState} from '../orchestrator/state.js';
import {ms} from '../orchestrator/time.js';
import {unbrokenFrom} from '../orchestrator/views.js';
import {type MessageNote, noteOf} from './messages.js';
import {type AgentName, agentNames} from './names.js';
import type {SpawnState} from './spawn-tree.js';

/** Who wrote a message. */
export type Party =
  | {readonly kind: 'operator'}
  /** The host itself: a reminder, a finished job, a stopped child. */
  | {readonly kind: 'host'}
  | {readonly kind: 'agent'; readonly agentId: string};

export type TurnOutcome = 'running' | 'done' | 'failed' | 'stopped';

export type Entry =
  | {
      readonly kind: 'turn';
      readonly key: string;
      readonly turnId: string;
      readonly ordinal: number;
      readonly atMs: number;
      /** What the host says the turn cost; null when it says nothing. */
      readonly costUsd: number | null;
      readonly outcome: TurnOutcome;
    }
  | {
      readonly kind: 'incoming';
      readonly key: string;
      readonly atMs: number;
      readonly from: Party;
      readonly note: MessageNote;
    }
  | {readonly kind: 'said'; readonly key: string; readonly text: string; readonly live: boolean}
  | {
      readonly kind: 'thought';
      readonly key: string;
      readonly ordinal: number;
      readonly text: string;
      /** How long the host saw it take: when it was last written less when it began; null when unknown or still going. */
      readonly spanMs: number | null;
      readonly live: boolean;
      /** The text a page left out, in bytes; read the item whole to see it. */
      readonly elidedBytes: number | null;
    }
  | {
      readonly kind: 'tool';
      readonly key: string;
      readonly ordinal: number;
      readonly call: ToolCall['call'];
      readonly state: 'running' | 'ok' | 'failed';
      /** How long it took (so far, while it runs); null when the host did not say when it began. */
      readonly spanMs: number | null;
      readonly output: string;
      readonly exitCode: number | null;
      readonly elidedBytes: number | null;
    }
  | {
      readonly kind: 'spawn';
      readonly key: string;
      readonly atMs: number;
      readonly childId: string;
    }
  | {
      readonly kind: 'join';
      readonly key: string;
      readonly atMs: number;
      readonly childId: string;
      readonly result: 'handed-back' | 'failed' | 'ended';
    };

export interface Crumb {
  readonly agentId: string;
  readonly name: AgentName;
}

export interface Transcript {
  readonly agentId: string;
  /** From the lead down to this agent. */
  readonly crumbs: readonly Crumb[];
  readonly state: SpawnState;
  readonly costUsd: number | null;
  /** How many turns the agent has taken, loaded or not. */
  readonly turns: number;
  readonly entries: readonly Entry[];
  /** Items of this agent below this ordinal are not loaded; null when all are. */
  readonly earlier: number | null;
}

const STATE: Readonly<Record<AgentStatus, SpawnState | 'live'>> = {
  [AgentStatus.UNSPECIFIED]: 'done',
  [AgentStatus.LAUNCHING]: 'live',
  [AgentStatus.ACTIVE]: 'live',
  [AgentStatus.WAITING]: 'live',
  [AgentStatus.IDLE]: 'done',
  [AgentStatus.ENDED]: 'done',
  [AgentStatus.FAILED]: 'failed',
  [AgentStatus.LOST]: 'failed',
};

const OUTCOME: Readonly<Record<TurnStatus, TurnOutcome>> = {
  [TurnStatus.UNSPECIFIED]: 'done',
  [TurnStatus.RUNNING]: 'running',
  [TurnStatus.COMPLETED]: 'done',
  [TurnStatus.INTERRUPTED]: 'stopped',
  [TurnStatus.FAILED]: 'failed',
};

/** The place of an agent in the tree: its ancestors from the root, then itself. */
function crumbsOf(
  state: OrchestratorState,
  agent: Agent,
  names: ReadonlyMap<string, AgentName>,
): Crumb[] {
  const path: Agent[] = [agent];
  const seen = new Set([agent.id]);
  for (
    let at = state.agents[agent.parentAgentId];
    at !== undefined;
    at = state.agents[at.parentAgentId]
  ) {
    if (seen.has(at.id)) break;
    seen.add(at.id);
    path.unshift(at);
  }
  return path.map(one => ({
    agentId: one.id,
    name: names.get(one.id) ?? {role: one.role, number: null},
  }));
}

/** An item's words: what is stored and any progress laid over it. */
function wordsOf(state: OrchestratorState, item: Item): {text: string; output: string} {
  const overlay = state.overlays[item.id];
  const {body} = item;
  switch (body.case) {
    case 'assistant':
    case 'reasoning':
    case 'prompt':
      return {text: body.value.text + (overlay?.text ?? ''), output: ''};
    case 'tool':
      return {text: '', output: body.value.output + (overlay?.commandOutput ?? '')};
    case undefined:
      return {text: '', output: ''};
    default:
      body satisfies never;
      return {text: '', output: ''};
  }
}

const spanOf = (item: Item, nowMs: number): number | null => {
  const startedMs = ms(item.startedAt);
  if (startedMs === null) return null;
  const endMs = item.status === ItemStatus.RUNNING ? nowMs : (ms(item.at) ?? startedMs);
  return Math.max(0, endMs - startedMs);
};

function toolEntry(state: OrchestratorState, item: Item, call: ToolCall, nowMs: number): Entry {
  const live = item.status === ItemStatus.RUNNING;
  return {
    kind: 'tool',
    key: `item:${item.id}`,
    ordinal: item.ordinal,
    call: call.call,
    state: live ? 'running' : call.isError || item.status === ItemStatus.FAILED ? 'failed' : 'ok',
    spanMs: spanOf(item, nowMs),
    output: call.output + (state.overlays[item.id]?.commandOutput ?? ''),
    exitCode: call.exitCode ?? null,
    elidedBytes: state.elided[item.id] ?? null,
  };
}

/** The entries one item makes: none for a prompt, which the message that started the turn replaces, or for empty words. */
function entriesOfItem(state: OrchestratorState, item: Item, nowMs: number): Entry[] {
  const {body} = item;
  const key = `item:${item.id}`;
  const live = item.status === ItemStatus.RUNNING;
  const elidedBytes = state.elided[item.id] ?? null;
  switch (body.case) {
    case 'tool':
      return [toolEntry(state, item, body.value, nowMs)];
    case 'reasoning': {
      const text = body.value.text + (state.overlays[item.id]?.text ?? '');
      if (text === '' && elidedBytes === null) return [];
      const spanMs = live ? null : spanOf(item, nowMs);
      return [{kind: 'thought', key, ordinal: item.ordinal, text, spanMs, live, elidedBytes}];
    }
    case 'assistant': {
      const text = body.value.text + (state.overlays[item.id]?.text ?? '');
      return text === '' ? [] : [{kind: 'said', key, text, live}];
    }
    case 'prompt':
    case undefined:
      return [];
    default:
      body satisfies never;
      return [];
  }
}

const partyOf = (message: Message, note: MessageNote): Party => {
  if (message.fromAgentId !== '') return {kind: 'agent', agentId: message.fromAgentId};
  switch (note.kind) {
    case 'text':
    case 'brief':
      return {kind: 'operator'};
    case 'handoff':
    case 'report':
    case 'job':
    case 'settled':
    case 'reminder':
    case 'limit':
      return {kind: 'host'};
    default:
      note satisfies never;
      return {kind: 'host'};
  }
};

/** What started a turn: the message it names, else the prompt the agent was given, else nothing. */
function incomingOf(state: OrchestratorState, turn: Turn, prompt: Item | undefined): Entry[] {
  const message = state.messages[turn.triggerMessageId];
  const key = `turn:${turn.id}:in`;
  if (message !== undefined) {
    const note = noteOf(message);
    return [{kind: 'incoming', key, atMs: ms(message.at) ?? 0, from: partyOf(message, note), note}];
  }
  if (prompt === undefined) return [];
  const text = wordsOf(state, prompt).text;
  return text === ''
    ? []
    : [
        {
          kind: 'incoming',
          key,
          atMs: ms(prompt.at) ?? 0,
          from: {kind: 'host'},
          note: {kind: 'text', text},
        },
      ];
}

/** The agents this one spawned and what came back from them, each with the moment it happened. */
function spawnEvents(
  state: OrchestratorState,
  agent: Agent,
): {readonly atMs: number; readonly entry: Entry}[] {
  const children = Object.values(state.agents).filter(child => child.parentAgentId === agent.id);
  const known = new Set(children.map(child => child.id));
  const events: {readonly atMs: number; readonly entry: Entry}[] = children.map(child => ({
    atMs: ms(child.startedAt) ?? 0,
    entry: {
      kind: 'spawn',
      key: `spawn:${child.id}`,
      atMs: ms(child.startedAt) ?? 0,
      childId: child.id,
    },
  }));
  const handedBack = new Set<string>();
  const messages = Object.values(state.messages)
    .filter(message => message.runId === agent.runId)
    .sort((a, b) => (ms(a.at) ?? 0) - (ms(b.at) ?? 0) || a.id.localeCompare(b.id));
  for (const message of messages) {
    const note = noteOf(message);
    const atMs = ms(message.at) ?? 0;
    if (
      note.kind === 'handoff' &&
      known.has(message.fromAgentId) &&
      message.toAgentId === agent.id
    ) {
      handedBack.add(message.fromAgentId);
      events.push({
        atMs,
        entry: {
          kind: 'join',
          key: `join:${message.id}`,
          atMs,
          childId: message.fromAgentId,
          result: 'handed-back',
        },
      });
    }
  }
  for (const message of messages) {
    const note = noteOf(message);
    if (note.kind !== 'settled' || !known.has(note.agentId) || handedBack.has(note.agentId))
      continue;
    const atMs = ms(message.at) ?? 0;
    const failed = STATE[note.status] === 'failed';
    events.push({
      atMs,
      entry: {
        kind: 'join',
        key: `join:${message.id}`,
        atMs,
        childId: note.agentId,
        result: failed ? 'failed' : 'ended',
      },
    });
  }
  return events;
}

/** The turns to show: those with loaded items, and any newer than all of them (or just the newest when none is loaded). */
function shownTurns(turns: readonly Turn[], loaded: ReadonlyMap<string, Item[]>): Turn[] {
  const withItems = turns.filter(turn => loaded.has(turn.id));
  const newest = turns[turns.length - 1];
  if (withItems.length === 0) return newest === undefined ? [] : [newest];
  const last = Math.max(...withItems.map(turn => turn.ordinal));
  return turns.filter(turn => loaded.has(turn.id) || turn.ordinal > last);
}

const startOfItem = (item: Item): number => ms(item.startedAt) ?? ms(item.at) ?? 0;

interface Event {
  readonly atMs: number;
  readonly entry: Entry;
}

/** One turn's entries: its divider, what started it, then its items with the events that fall among them. */
function entriesOfTurn(
  state: OrchestratorState,
  turn: Turn,
  own: readonly Item[],
  events: readonly Event[],
  nowMs: number,
): Entry[] {
  const started = state.messages[turn.triggerMessageId] !== undefined;
  const head: Entry[] = [
    {
      kind: 'turn',
      key: `turn:${turn.id}`,
      turnId: turn.id,
      ordinal: turn.ordinal,
      atMs: ms(turn.startedAt) ?? 0,
      costUsd: turn.usage?.costUsd ?? null,
      outcome: OUTCOME[turn.status],
    },
    ...incomingOf(state, turn, started ? undefined : own.find(item => item.body.case === 'prompt')),
  ];
  const body: Entry[] = [];
  let next = 0;
  for (const item of own) {
    // An event comes after every item that began at or before it.
    while (next < events.length && (events[next]?.atMs ?? 0) < startOfItem(item)) {
      body.push((events[next++] as Event).entry);
    }
    body.push(...entriesOfItem(state, item, nowMs));
  }
  return [...head, ...body, ...events.slice(next).map(event => event.entry)];
}

/** The transcript of one agent, or null when the agent is not in the state. */
export function transcript(
  state: OrchestratorState,
  agentId: string,
  nowMs: number,
): Transcript | null {
  const agent = state.agents[agentId];
  if (agent === undefined) return null;
  const turns = Object.values(state.turns)
    .filter(turn => turn.agentId === agentId)
    .sort((a, b) => a.ordinal - b.ordinal);
  const items = Object.values(state.items)
    .filter(item => item.agentId === agentId)
    .sort((a, b) => a.ordinal - b.ordinal);
  const loaded = new Map<string, Item[]>();
  for (const item of items) loaded.set(item.turnId, [...(loaded.get(item.turnId) ?? []), item]);
  const shown = shownTurns(turns, loaded);
  const events = spawnEvents(state, agent).sort((a, b) => a.atMs - b.atMs);
  // Each event goes to the last turn shown that began at or before it, else the first.
  const starts = shown.map(turn => ms(turn.startedAt) ?? 0);
  const turnOf = (atMs: number): number =>
    starts.reduce((found, start, index) => (start <= atMs ? index : found), 0);
  const entries = shown.flatMap((turn, index) =>
    entriesOfTurn(
      state,
      turn,
      loaded.get(turn.id) ?? [],
      events.filter(event => turnOf(event.atMs) === index),
      nowMs,
    ),
  );
  const from = unbrokenFrom(state, agentId);
  const span = STATE[agent.status];
  return {
    agentId,
    crumbs: crumbsOf(state, agent, agentNames(state, agent.runId)),
    state: span === 'live' ? 'running' : span,
    costUsd: agent.usage?.costUsd ?? null,
    turns: turns.length,
    entries,
    earlier: from === null || from <= 1 ? null : from,
  };
}
