/**
 * One agent's session as a conversation, top to bottom: for each turn a divider, the message that
 * started it, then the agent's thinking, words and tool calls in order, with a row where it
 * spawned an agent and where one joined back. A pure function of the state and `nowMs` (no clock
 * here); the screens turn each entry into words.
 *
 * Properties the tests hold (`transcript.properties.test.ts`), for any items, turns, messages and
 * children, loaded in any amount:
 * - every loaded item with visible content appears exactly once, in item order; items whose turn
 *   record was not loaded sit below an explicit unavailable-turn divider, and a prompt item shows
 *   only where no message started its turn or its turn record is unavailable;
 * - the turns shown are in order, each divider is followed by that turn's entries only, and a turn
 *   with no loaded items is shown only when it is newer than every loaded one;
 * - a spawn or join row appears once per child and per result, after the divider of the last turn
 *   that began before it (the first turn, when none had), and never between the entries of a turn
 *   out of time order;
 * - a join of a child that did not hand back says how the host settled it (idle, ended, failed or
 *   lost) and carries the host's reason;
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
import {itemIs} from '../orchestrator/items.js';
import {type MessageNote, noteOf} from '../orchestrator/messages.js';
import type {OrchestratorState} from '../orchestrator/state.js';
import {ms} from '../orchestrator/time.js';
import {unbrokenFrom} from '../orchestrator/views.js';
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
      readonly kind: 'turn-unavailable';
      readonly key: string;
      readonly turnId: string;
      /** Loaded items attributed to this turn, including ones with no visible body. */
      readonly items: number;
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
      readonly call: ToolCall;
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
      /** How the child came back: with a handoff, or settled without one, as the host said it did. */
      readonly result: 'handed-back' | Settled;
      /** The host's reason a child settled; empty when it gave none, and for a handoff. */
      readonly detail: string;
    };

/** How a child stopped working without a handoff. */
type Settled = 'idle' | 'ended' | 'failed' | 'lost';

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

/** The host settles a child as idle, ended, failed or lost; a status it does not send reads as ended. */
const SETTLED: Readonly<Record<AgentStatus, Settled>> = {
  [AgentStatus.UNSPECIFIED]: 'ended',
  [AgentStatus.LAUNCHING]: 'ended',
  [AgentStatus.ACTIVE]: 'ended',
  [AgentStatus.WAITING]: 'ended',
  [AgentStatus.IDLE]: 'idle',
  [AgentStatus.ENDED]: 'ended',
  [AgentStatus.FAILED]: 'failed',
  [AgentStatus.LOST]: 'lost',
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

/** Where an agent is in the tree, from the lead down to itself. */
export function agentPlace(state: OrchestratorState, agentId: string): Crumb[] {
  const agent = state.agents[agentId];
  return agent === undefined ? [] : crumbsOf(state, agent, agentNames(state, agent.runId));
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
    call,
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

/** A prompt whose turn record is unavailable: the surviving input to that turn, when it has words. */
function entryOfUnavailablePrompt(state: OrchestratorState, item: Item): Entry[] {
  if (!itemIs(item, 'prompt')) return [];
  const text = wordsOf(state, item).text;
  return text === ''
    ? []
    : [
        {
          kind: 'incoming',
          key: `item:${item.id}:prompt`,
          atMs: ms(item.at) ?? 0,
          from: {kind: 'host'},
          note: {kind: 'text', text},
        },
      ];
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
    case 'report-due':
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
          detail: '',
        },
      });
    }
  }
  for (const message of messages) {
    const note = noteOf(message);
    if (note.kind !== 'settled' || !known.has(note.agentId) || handedBack.has(note.agentId))
      continue;
    const atMs = ms(message.at) ?? 0;
    events.push({
      atMs,
      entry: {
        kind: 'join',
        key: `join:${message.id}`,
        atMs,
        childId: note.agentId,
        result: SETTLED[note.status],
        detail: note.detail,
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

type TurnSegment =
  | {readonly kind: 'available'; readonly turn: Turn; readonly own: readonly Item[]}
  | {readonly kind: 'unavailable'; readonly turnId: string; readonly own: readonly Item[]};

const startOfSegment = (segment: TurnSegment): number => {
  if (segment.kind === 'available') return ms(segment.turn.startedAt) ?? 0;
  const first = segment.own[0];
  return first === undefined ? 0 : startOfItem(first);
};

/**
 * The loaded turn groups to draw, ordered by agent-global item ordinal. Empty known turns follow
 * loaded known turns and sit among unavailable groups by recorded start. Missing turn records
 * cannot be placed in turn-ordinal space, so their item ordinal orders them relative to each other.
 */
function turnSegments(turns: readonly Turn[], loaded: ReadonlyMap<string, Item[]>): TurnSegment[] {
  const available = new Set(turns.map(turn => turn.id));
  const shown: Extract<TurnSegment, {readonly kind: 'available'}>[] = shownTurns(turns, loaded).map(
    turn => ({
      kind: 'available',
      turn,
      own: loaded.get(turn.id) ?? [],
    }),
  );
  const unavailable = [...loaded.entries()]
    .filter(([turnId]) => !available.has(turnId))
    .map<TurnSegment>(([turnId, own]) => ({kind: 'unavailable', turnId, own}));
  const withItems = [...shown.filter(segment => segment.own.length > 0), ...unavailable].sort(
    (a, b) => (a.own[0]?.ordinal ?? 0) - (b.own[0]?.ordinal ?? 0),
  );
  const result = [...withItems];
  let afterKnownItems =
    result.reduce(
      (last, segment, index) =>
        segment.kind === 'available' && segment.own.length > 0 ? index : last,
      -1,
    ) + 1;
  for (const empty of shown.filter(segment => segment.own.length === 0)) {
    const began = ms(empty.turn.startedAt) ?? 0;
    const next = result.findIndex(
      (segment, index) => index >= afterKnownItems && startOfSegment(segment) > began,
    );
    const at = next === -1 ? result.length : next;
    result.splice(at, 0, empty);
    afterKnownItems = at + 1;
  }
  return result;
}

/** Items and the agent-tree events that happened among them. */
function entriesAmongItems(
  state: OrchestratorState,
  own: readonly Item[],
  events: readonly Event[],
  nowMs: number,
  unavailable: boolean,
): Entry[] {
  const body: Entry[] = [];
  let next = 0;
  for (const item of own) {
    // An event comes after every item that began at or before it.
    while (next < events.length && (events[next]?.atMs ?? 0) < startOfItem(item)) {
      body.push((events[next++] as Event).entry);
    }
    if (unavailable) body.push(...entryOfUnavailablePrompt(state, item));
    body.push(...entriesOfItem(state, item, nowMs));
  }
  return [...body, ...events.slice(next).map(event => event.entry)];
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
    ...incomingOf(state, turn, started ? undefined : own.find(item => itemIs(item, 'prompt'))),
  ];
  return [...head, ...entriesAmongItems(state, own, events, nowMs, false)];
}

/** Loaded items whose turn record was not in the run history window. */
function entriesOfUnavailableTurn(
  state: OrchestratorState,
  turnId: string,
  own: readonly Item[],
  events: readonly Event[],
  nowMs: number,
): Entry[] {
  return [
    {
      kind: 'turn-unavailable',
      key: `turn-unavailable:${turnId}`,
      turnId,
      items: own.length,
    },
    ...entriesAmongItems(state, own, events, nowMs, true),
  ];
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
  const segments = turnSegments(turns, loaded);
  const events = spawnEvents(state, agent).sort((a, b) => a.atMs - b.atMs);
  // Each event goes to the most recently begun segment at that moment. Visual order can differ
  // from start-time order when bounded history omits a turn record, so array position is not time.
  const starts = segments.map(startOfSegment);
  const turnOf = (atMs: number): number => {
    const eligible = starts.reduce(
      (found, start, index) =>
        start <= atMs && (found === -1 || start >= (starts[found] ?? Number.NEGATIVE_INFINITY))
          ? index
          : found,
      -1,
    );
    if (eligible !== -1) return eligible;
    return starts.reduce(
      (first, start, index) =>
        start < (starts[first] ?? Number.POSITIVE_INFINITY) ? index : first,
      0,
    );
  };
  const entries = segments.flatMap((segment, index) => {
    const ownEvents = events.filter(event => turnOf(event.atMs) === index);
    return segment.kind === 'available'
      ? entriesOfTurn(state, segment.turn, segment.own, ownEvents, nowMs)
      : entriesOfUnavailableTurn(state, segment.turnId, segment.own, ownEvents, nowMs);
  });
  const from = unbrokenFrom(state, agentId);
  const span = STATE[agent.status];
  return {
    agentId,
    crumbs: agentPlace(state, agentId),
    state: span === 'live' ? 'running' : span,
    costUsd: agent.usage?.costUsd ?? null,
    turns: turns.length,
    entries,
    earlier: from === null || from <= 1 ? null : from,
  };
}
