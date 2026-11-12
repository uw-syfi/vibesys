import {AgentStatus, RunLimit} from '@vibesys/orchestrator-proto';
import {Conversation, type ConversationItem} from '@vibesys/ui';
import type {JSX} from 'react';
import type {HistoryStatus} from '../../core/index.js';
import {
  type AgentName,
  agentNames,
  type Entry,
  type MessageNote,
  type OrchestratorState,
  type Party,
  type Transcript as TranscriptData,
  transcript,
} from '../../state/index.js';
import {useDisplayClock} from '../display-clock.js';
import type {DisplayClock} from '../display-time.js';
import {describeCall, jobWord} from '../orchestrator/words.js';
import {agentLabel} from './labels.js';
import {formatSpan, formatUsd} from './time.js';

/** What the screen reads of the agent's history requests, and how it makes them. */
export interface HistoryAccess {
  readonly earlier: HistoryStatus;
  /** Reading each item whole, by ordinal. */
  readonly wholes: Readonly<Record<number, HistoryStatus>>;
  readonly loadEarlier: (before: number) => void;
  readonly expand: (ordinal: number) => void;
}

const LIMIT_WORD: Readonly<Record<RunLimit, string>> = {
  [RunLimit.UNSPECIFIED]: 'The run reached a limit.',
  [RunLimit.TIME]: 'The run reached its time limit.',
  [RunLimit.COST]: 'The run reached its cost limit.',
};

const bytesWord = (bytes: number): string =>
  bytes < 1024 ? `${bytes} B` : `${Math.round(bytes / 1024)} KB`;

/** What a child did when it settled without a handoff; the host settles one only as these four. */
const SETTLED_WORD: Readonly<Record<AgentStatus, string>> = {
  [AgentStatus.UNSPECIFIED]: 'stopped',
  [AgentStatus.LAUNCHING]: 'stopped',
  [AgentStatus.ACTIVE]: 'stopped',
  [AgentStatus.WAITING]: 'stopped',
  [AgentStatus.IDLE]: 'went idle',
  [AgentStatus.ENDED]: 'ended',
  [AgentStatus.FAILED]: 'failed',
  [AgentStatus.LOST]: 'was lost',
};

/** The host's reason, after the words it explains; just a full stop when it gave none. */
const because = (detail: string): string => (detail.trim() === '' ? '.' : `: ${detail.trim()}`);

/** What a message that started a turn says, in words. */
function noteWords(note: MessageNote, labelOf: (agentId: string) => string): string {
  switch (note.kind) {
    case 'text':
    case 'brief':
      return note.text;
    case 'handoff':
      return note.claim;
    case 'report':
      return note.headline;
    case 'job':
      return `Job ${note.jobId}: ${jobWord(note.outcome).label}${note.detail === '' ? '' : `. ${note.detail}`}`;
    case 'settled':
      return `${labelOf(note.agentId)} ${SETTLED_WORD[note.status]} without a handoff${because(note.detail)}`;
    case 'reminder':
      return 'A reminder to finish what it still owes.';
    case 'report-due':
      return 'Asked for a progress report.';
    case 'limit':
      return LIMIT_WORD[note.limit];
    default:
      note satisfies never;
      return '';
  }
}

interface Context {
  readonly agentId: string;
  readonly host: string;
  readonly labelOf: (agentId: string) => string;
  readonly open: (agentId: string) => void;
  readonly history: HistoryAccess;
  readonly clock: DisplayClock;
}

function author(party: Party, context: Context): {label: string; press?: () => void} {
  switch (party.kind) {
    case 'operator':
      return {label: 'You'};
    case 'host':
      return {label: 'VibeSys'};
    case 'agent':
      return {label: context.labelOf(party.agentId), press: () => context.open(party.agentId)};
    default:
      party satisfies never;
      return {label: ''};
  }
}

/** Why a body is not here, as the line that stands in for it: being read, or waiting to be. */
function notice(bytes: number | null, ordinal: number, context: Context): string | undefined {
  if (bytes === null) return undefined;
  const read = context.history.wholes[ordinal];
  if (read?._tag === 'Loading') return `Loading ${bytesWord(bytes)}`;
  if (read?._tag === 'Failed') return `Could not load ${bytesWord(bytes)}. Open it again to retry.`;
  return `${bytesWord(bytes)} not loaded`;
}

const OUTCOME = {
  running: ' · running',
  done: '',
  failed: ' · failed',
  stopped: ' · stopped',
} as const;

type Of<K extends Entry['kind']> = Extract<Entry, {kind: K}>;

function turnItem(entry: Of<'turn'>, clock: DisplayClock): ConversationItem {
  const cost = entry.costUsd === null ? '' : ` · ${formatUsd(entry.costUsd)}`;
  return {
    kind: 'rule',
    id: entry.key,
    label: `Turn ${entry.ordinal} · ${clock.dayTime(entry.atMs)}${cost}${OUTCOME[entry.outcome]}`,
  };
}

function unavailableTurnItem(entry: Of<'turn-unavailable'>): ConversationItem {
  return {
    kind: 'rule',
    id: entry.key,
    label: `Turn details not loaded · ${entry.items} ${entry.items === 1 ? 'item' : 'items'}`,
  };
}

function thoughtItem(entry: Of<'thought'>, context: Context): ConversationItem {
  const span = entry.spanMs === null ? 'Thought' : `Thought for ${formatSpan(entry.spanMs)}`;
  const waiting = notice(entry.elidedBytes, entry.ordinal, context);
  return {
    kind: 'thought',
    id: entry.key,
    label: entry.live ? 'Thinking' : span,
    text: entry.text,
    ...(waiting === undefined ? {} : {notice: waiting}),
  };
}

function toolItem(entry: Of<'tool'>, context: Context): ConversationItem {
  const words = describeCall(entry.call);
  const waiting = notice(entry.elidedBytes, entry.ordinal, context);
  const unwritten = waiting !== undefined || (entry.state === 'running' && entry.output === '');
  return {
    kind: 'tool',
    id: entry.key,
    name: words.name,
    state: entry.state === 'running' ? 'pending' : entry.state,
    summary: (words.asked ?? '').split('\n')[0] ?? '',
    ...(entry.spanMs === null ? {} : {duration: formatSpan(entry.spanMs)}),
    input: words.asked,
    output: unwritten ? null : entry.output,
    ...(waiting === undefined ? {} : {notice: waiting}),
  };
}

const JOINED = {
  'handed-back': {label: 'Done', tone: 'ok'},
  idle: {label: 'Idle', tone: 'warn'},
  ended: {label: 'Ended', tone: 'neutral'},
  failed: {label: 'Failed', tone: 'err'},
  lost: {label: 'Lost', tone: 'err'},
} as const;

function joinItem(entry: Of<'join'>, context: Context): ConversationItem {
  const detail = entry.detail.trim();
  return {
    kind: 'link',
    id: entry.key,
    icon: 'arrow-up',
    title: `${context.labelOf(entry.childId)} joined`,
    chip: JOINED[entry.result],
    trail: context.clock.time(entry.atMs),
    ...(detail === '' ? {} : {note: detail}),
    press: () => context.open(entry.childId),
  };
}

function itemOf(entry: Entry, context: Context): ConversationItem[] {
  switch (entry.kind) {
    case 'turn':
      return [turnItem(entry, context.clock)];
    case 'turn-unavailable':
      return [unavailableTurnItem(entry)];
    case 'incoming':
      return [
        {
          kind: 'bubble',
          id: entry.key,
          author: author(entry.from, context),
          at: context.clock.time(entry.atMs),
          text: noteWords(entry.note, context.labelOf),
        },
      ];
    case 'said':
      return [{kind: 'said', id: entry.key, text: entry.text}];
    case 'thought':
      return [thoughtItem(entry, context)];
    case 'tool':
      return [toolItem(entry, context)];
    case 'spawn':
      return [
        {
          kind: 'link',
          id: entry.key,
          icon: 'bot',
          title: `Spawned ${context.labelOf(entry.childId)}`,
          trail: `${context.host} · ${context.clock.time(entry.atMs)}`,
          press: () => context.open(entry.childId),
        },
      ];
    case 'join':
      return [joinItem(entry, context)];
    default:
      entry satisfies never;
      return [];
  }
}

/** The names an agent shows for the ones it points at. */
function labelsOf(names: ReadonlyMap<string, AgentName>): (agentId: string) => string {
  return agentId => {
    const name = names.get(agentId);
    return name === undefined ? 'An agent' : agentLabel(name);
  };
}

/** The items of a transcript, one per row it draws. */
export function itemsOf(data: TranscriptData, context: Context): ConversationItem[] {
  return data.entries.flatMap(entry => itemOf(entry, context));
}

/**
 * One agent's session as a conversation, newest at the bottom: a rule per turn, what started it,
 * the thinking, words and tool calls, and a row where an agent was spawned or came back. Older
 * turns are read as the reader scrolls up; a large body is read when its row is opened.
 */
export function AgentTranscript(props: {
  readonly state: OrchestratorState;
  readonly agentId: string;
  readonly host: string;
  readonly nowMs: number;
  readonly history: HistoryAccess;
  readonly open: (agentId: string) => void;
}): JSX.Element | null {
  const {state, agentId, history} = props;
  const clock = useDisplayClock();
  const data = transcript(state, agentId, props.nowMs);
  const agent = state.agents[agentId];
  if (data === null || agent === undefined) return null;
  const context: Context = {
    agentId,
    host: props.host,
    labelOf: labelsOf(agentNames(state, agent.runId)),
    open: props.open,
    history,
    clock,
  };
  const items = itemsOf(data, context);
  const ordinals = new Map(
    data.entries.flatMap(entry =>
      entry.kind === 'thought' || entry.kind === 'tool'
        ? [[entry.key, {ordinal: entry.ordinal, elided: entry.elidedBytes !== null}] as const]
        : [],
    ),
  );
  const earlier = data.earlier;
  return (
    <Conversation
      label={`${labelsOf(agentNames(state, agent.runId))(agentId)}, turn by turn`}
      items={items}
      size="fill"
      earlier={
        earlier === null
          ? null
          : {
              text: history.earlier._tag === 'Failed' ? 'Retry earlier turns' : 'Earlier turns',
              loading: history.earlier._tag === 'Loading',
              load: () => history.loadEarlier(earlier),
            }
      }
      opened={(id, open) => {
        const row = ordinals.get(id);
        if (open && row?.elided === true) history.expand(row.ordinal);
      }}
    />
  );
}
