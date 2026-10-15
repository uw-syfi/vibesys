/**
 * The inspector's logs: an XState machine (docs/contributing/clients-architecture.md, "State") over
 * one inspected run's log streams, live or ended (`docs/contributing/desktop-connection.md`,
 * "Logs").
 *
 * `closed` until `open` names a run; then `open` is two regions. `list` asks for the run's streams
 * on entry and on `refresh`, retrying a transient failure after a delay; `follow` invokes one
 * `logFollowMachine` for the stream on screen: the user's pick, else the run's narration, else the
 * newest. `close`, or opening another run, leaves `open`, which stops the listing and the follower
 * (its read in flight aborts), so nothing is fetched for a stream that is not shown. The operations
 * (`list`, and the follower's read) are provided by the shell.
 */
import type {LogStream} from '@vibesys/contracts/host-helper';
import type {HostKey} from '@vibesys/hosts';
import {
  type LogFollowConfig,
  type LogLine,
  type LogListOutcome,
  type LogReadFailure,
  type LogView,
  logFollowMachine,
  logsUnsupported,
  logViewComplete,
  logViewRecords,
} from '@vibesys/logs';
import {unprovided} from '@vibesys/machine';
import {assign, raise, type SnapshotFrom, sendTo, setup} from 'xstate';
import {INSPECTOR_CLOSED, type InspectorAction, type InspectorState} from './inspector.js';

/** What the streams are grouped by on screen, in this order. */
export type LogGroup =
  | 'narration'
  | 'startup'
  | 'gates'
  | 'invocations'
  | 'tools'
  | 'slurm'
  | 'other';

const GROUP_ORDER: readonly LogGroup[] = [
  'narration',
  'startup',
  'gates',
  'invocations',
  'tools',
  'slurm',
  'other',
];

const GROUP_LABELS: Readonly<Record<LogGroup, string>> = {
  narration: 'Run narration',
  startup: 'Startup',
  gates: 'Gates',
  invocations: 'Agent invocations',
  tools: 'Tool builds',
  slurm: 'Slurm jobs',
  other: 'Other',
};

/** A stream's state: still written, or how it closed. */
export type LogStreamStatus = 'running' | 'ended' | 'failed' | 'abandoned';

/** One stream as the inspector lists it. */
export interface InspectorStream {
  readonly id: string;
  readonly group: LogGroup;
  /** What the stream is for, in words: "Startup / building image", "Gate accuracy (attempt 2)". */
  readonly label: string;
  readonly status: LogStreamStatus;
  /** The stream is JSON narration records, which render as records, not raw text. */
  readonly narration: boolean;
}

export interface LogStreamGroup {
  readonly group: LogGroup;
  readonly label: string;
  /** Oldest first, as the run's store lists them. */
  readonly streams: readonly InspectorStream[];
}

function text(value: unknown): string | null {
  return typeof value === 'string' && value !== '' ? value : null;
}

function words(value: string): string {
  return value.replaceAll('_', ' ');
}

/** The group and label of a stream's owner; an owner this view does not know shows its kind. */
function described(owner: LogStream['owner']): {readonly group: LogGroup; readonly label: string} {
  const field = (key: string): string | null => text(owner[key]);
  switch (owner['kind']) {
    case 'run_narration':
      return {group: 'narration', label: 'Run narration'};
    case 'startup_step':
      return {group: 'startup', label: `Startup / ${words(field('stage') ?? 'unknown step')}`};
    case 'gate_stage': {
      const attempt = owner['attempt'];
      const gate = field('gate') ?? 'unknown';
      const execution = field('execution_id');
      const base =
        typeof attempt === 'number' ? `Gate ${gate} (attempt ${attempt})` : `Gate ${gate}`;
      return {group: 'gates', label: execution === null ? base : `${base}, ${execution}`};
    }
    case 'agent_invocation':
      return {
        group: 'invocations',
        label: `Agent invocation ${field('invocation_id') ?? ''}`.trim(),
      };
    case 'tool_build':
      return {group: 'tools', label: `Tool build / ${field('name') ?? 'unknown tool'}`};
    case 'slurm_job':
      return {group: 'slurm', label: `Slurm job ${field('job_id') ?? ''}`.trim()};
    default:
      return {group: 'other', label: words(text(owner['kind']) ?? 'unknown owner')};
  }
}

/** `streams` as the inspector lists them. */
export function inspectorStreams(streams: readonly LogStream[]): InspectorStream[] {
  return streams.map(stream => {
    const {group, label} = described(stream.owner);
    return {
      id: stream.id,
      group,
      label,
      status: stream.outcome ?? 'running',
      narration: group === 'narration',
    };
  });
}

/** `streams` grouped for the picker; empty groups are left out. */
export function groupedStreams(streams: readonly InspectorStream[]): LogStreamGroup[] {
  return GROUP_ORDER.flatMap(group => {
    const members = streams.filter(stream => stream.group === group);
    return members.length === 0 ? [] : [{group, label: GROUP_LABELS[group], streams: members}];
  });
}

/** The run whose logs are shown. */
export interface InspectorLogsTarget {
  readonly host: HostKey;
  readonly instanceId: string;
}

export type InspectorLogsEvent =
  | {readonly type: 'open'; readonly target: InspectorLogsTarget}
  | {readonly type: 'close'}
  | {readonly type: 'select'; readonly streamId: string}
  | {readonly type: 'refresh'};

type InternalEvent = {readonly type: 'retarget'};

export interface InspectorLogsInput {
  /** How long a failed listing waits before it asks again. */
  readonly retryMs?: number;
  readonly follow?: LogFollowConfig;
}

export interface InspectorLogsContext {
  readonly target: InspectorLogsTarget | null;
  readonly streams: readonly InspectorStream[];
  /** The stream the user picked; null shows the default. */
  readonly chosen: string | null;
  /** The stream the follower reads. */
  readonly followed: string | null;
  readonly listFailure: LogReadFailure | null;
  readonly retryMs: number;
  readonly follow: LogFollowConfig | undefined;
}

const DEFAULT_RETRY_MS = 2000;

/** The stream on screen: the user's pick while it is listed, else the narration, else the newest. */
export function shownStream(context: InspectorLogsContext): string | null {
  const {streams, chosen} = context;
  if (chosen !== null && streams.some(stream => stream.id === chosen)) return chosen;
  const narration = streams.filter(stream => stream.narration).at(-1);
  return narration?.id ?? streams.at(-1)?.id ?? null;
}

function listed(event: unknown): LogListOutcome | undefined {
  return (event as {output?: LogListOutcome}).output;
}

export const inspectorLogsMachine = setup({
  types: {
    context: {} as InspectorLogsContext,
    events: {} as InspectorLogsEvent | InternalEvent,
    input: {} as InspectorLogsInput,
  },
  actors: {
    list: unprovided<LogListOutcome, InspectorLogsTarget>('log list'),
    follow: logFollowMachine,
  },
  guards: {
    unsupported: ({event}) => {
      const output = listed(event);
      return output !== undefined && !output.ok && logsUnsupported(output.failure);
    },
    failed: ({event}) => listed(event)?.ok === false,
    hasShown: ({context}) => context.target !== null && shownStream(context) !== null,
    moved: ({context}) => shownStream(context) !== context.followed,
  },
  actions: {
    begin: assign((_, params: {target: InspectorLogsTarget}) => ({
      target: params.target,
      streams: [],
      chosen: null,
      followed: null,
      listFailure: null,
    })),
    end: assign({target: null, streams: [], chosen: null, followed: null, listFailure: null}),
    listed: assign(({event}) => {
      const output = listed(event);
      if (output === undefined || !output.ok) return {};
      return {streams: inspectorStreams(output.streams), listFailure: null};
    }),
    listFailed: assign(({event}) => {
      const settled = event as {output?: LogListOutcome; error?: unknown};
      if (settled.output !== undefined && !settled.output.ok)
        return {listFailure: settled.output.failure};
      const message =
        settled.error instanceof Error ? settled.error.message : String(settled.error);
      return {listFailure: {code: 'transport' as const, message}};
    }),
    choose: assign((_, params: {streamId: string}) => ({chosen: params.streamId})),
    markFollowed: assign(({context}) => ({followed: shownStream(context)})),
    unfollow: assign({followed: null}),
    openFollower: sendTo('follow', {type: 'open'}),
    retarget: raise({type: 'retarget'}),
  },
  delays: {
    retryList: ({context}) => context.retryMs,
  },
}).createMachine({
  id: 'inspectorLogs',
  context: ({input}) => ({
    target: null,
    streams: [],
    chosen: null,
    followed: null,
    listFailure: null,
    retryMs: input.retryMs ?? DEFAULT_RETRY_MS,
    follow: input.follow,
  }),
  initial: 'closed',
  on: {
    open: {
      target: '.open',
      reenter: true,
      actions: {type: 'begin', params: ({event}) => ({target: event.target})},
    },
    close: {target: '.closed', actions: 'end'},
  },
  states: {
    closed: {},
    open: {
      type: 'parallel',
      on: {
        select: {
          actions: [
            {type: 'choose', params: ({event}) => ({streamId: event.streamId})},
            'retarget',
          ],
        },
      },
      states: {
        list: {
          initial: 'loading',
          states: {
            loading: {
              invoke: {
                id: 'list',
                src: 'list',
                input: ({context}) => context.target as InspectorLogsTarget,
                onDone: [
                  {guard: 'unsupported', target: 'unsupported', actions: 'listFailed'},
                  {guard: 'failed', target: 'failed', actions: 'listFailed'},
                  {target: 'loaded', actions: ['listed', 'retarget']},
                ],
                onError: {target: 'failed', actions: 'listFailed'},
              },
            },
            loaded: {on: {refresh: 'loading'}},
            failed: {
              on: {refresh: 'loading'},
              after: {retryList: 'loading'},
            },
            unsupported: {},
          },
        },
        follow: {
          initial: 'deciding',
          states: {
            deciding: {
              always: [
                {guard: 'hasShown', target: 'following', actions: 'markFollowed'},
                {target: 'waiting', actions: 'unfollow'},
              ],
            },
            waiting: {on: {retarget: 'deciding'}},
            following: {
              entry: 'openFollower',
              on: {retarget: {guard: 'moved', target: 'deciding'}},
              invoke: {
                id: 'follow',
                src: 'follow',
                input: ({context}) => ({
                  instanceId: context.target?.instanceId ?? '',
                  streamId: context.followed ?? '',
                  ...(context.follow === undefined ? {} : {config: context.follow}),
                }),
              },
            },
          },
        },
      },
    },
  },
});

export type InspectorLogsSnapshot = SnapshotFrom<typeof inspectorLogsMachine>;

/** The follower's part of the view; the shell reads it from the invoked follower's snapshot. */
export interface LogFollowerView {
  readonly view: LogView;
  readonly failure: LogReadFailure | null;
}

/** One line on screen, numbered from the start of what the view ever held. */
export interface ShownLine {
  readonly number: number;
  readonly line: LogLine;
}

/** What the sheet's logs section renders. */
export interface InspectorLogsView {
  readonly open: boolean;
  /** The listing's state; `unsupported` when the host offers no logs. */
  readonly listing: 'loading' | 'loaded' | 'failed' | 'unsupported';
  readonly listFailure: LogReadFailure | null;
  readonly groups: readonly LogStreamGroup[];
  /** The stream on screen. */
  readonly shown: InspectorStream | null;
  /** The shown stream's lines, its unfinished last line included. */
  readonly lines: readonly ShownLine[];
  /** Earlier output of the shown stream exists that the view does not hold. */
  readonly earlier: boolean;
  /** The view holds every byte the shown stream will have. */
  readonly complete: boolean;
  /** The shown stream's last read failure; cleared by its next chunk. */
  readonly readFailure: LogReadFailure | null;
}

export const INSPECTOR_LOGS_CLOSED: InspectorLogsView = {
  open: false,
  listing: 'loading',
  listFailure: null,
  groups: [],
  shown: null,
  lines: [],
  earlier: false,
  complete: false,
  readFailure: null,
};

function listingOf(snapshot: InspectorLogsSnapshot): InspectorLogsView['listing'] {
  for (const state of ['unsupported', 'failed', 'loaded'] as const) {
    if (snapshot.matches({open: {list: state}})) return state;
  }
  return 'loading';
}

/** The logs section's view of `snapshot`, with the follower's state when one runs. */
export function inspectorLogsView(
  snapshot: InspectorLogsSnapshot,
  follower: LogFollowerView | null,
): InspectorLogsView {
  if (!snapshot.matches('open')) return INSPECTOR_LOGS_CLOSED;
  const {context} = snapshot;
  const view = follower?.view ?? null;
  const shownId = context.followed ?? shownStream(context);
  return {
    open: true,
    listing: listingOf(snapshot),
    listFailure: context.listFailure,
    groups: groupedStreams(context.streams),
    shown: context.streams.find(stream => stream.id === shownId) ?? null,
    lines:
      view === null
        ? []
        : logViewRecords(view).map((line, index) => ({number: view.evictedLines + index, line})),
    earlier: view !== null && (view.evictedLines > 0 || (view.start ?? 0) > 0),
    complete: view !== null && logViewComplete(view),
    readFailure: follower?.failure ?? null,
  };
}

/** What the logs section says instead of, or above, the lines; null when there is nothing to say. */
export function inspectorLogsNotice(view: InspectorLogsView): string | null {
  switch (view.listing) {
    case 'unsupported':
      return "This host's VibeSys does not serve logs; update it there to see this run's logs.";
    case 'failed':
      return `Could not list this run's logs (${view.listFailure?.message ?? 'unknown error'}); retrying.`;
    case 'loading':
      if (view.shown === null) return 'Looking for logs…';
      break;
    case 'loaded':
      if (view.shown === null) return 'This run recorded no logs.';
      break;
  }
  if (view.readFailure !== null) return `Could not read this log: ${view.readFailure.message}`;
  return null;
}

/** The sheet's whole state: the run's identity, and its logs. */
export type InspectorSheetState = InspectorState & {readonly logs: InspectorLogsView};

/** What the sheet sends: the inspector's actions, and the logs section's own. */
export type InspectorSheetAction =
  | InspectorAction
  | {readonly type: 'select-log'; readonly streamId: string};

export const INSPECTOR_SHEET_CLOSED: InspectorSheetState = {
  ...INSPECTOR_CLOSED,
  logs: INSPECTOR_LOGS_CLOSED,
};
