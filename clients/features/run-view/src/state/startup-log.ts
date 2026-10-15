/**
 * The startup log panel's control state: an XState machine (docs/contributing/clients-architecture.md,
 * "State") over a run's startup log streams (owner `startup_step`).
 *
 * `hidden` until the run is starting or failed while starting (`startupLogObservation`); then
 * `shown`, expanded at first. Expanded is two regions: `list` asks for the run's streams on entry and
 * again whenever the startup stage changes, retrying a transient failure after a delay; `follow`
 * invokes one `logFollowMachine` for the stream on screen, the newest unless the user picked
 * another. Collapsing or hiding leaves `expanded`, which stops the listing and the follower (its
 * read in flight aborts), so nothing is fetched while the panel is collapsed. The operations (`list`,
 * and the follower's read) are provided by the shell over a `RunLogSource`.
 */
import type {LogStream} from '@vibesys/contracts/host-helper';
import {
  type LogFollowConfig,
  type LogListOutcome,
  type LogReadFailure,
  type LogView,
  logFollowMachine,
  logsUnsupported,
  logViewLines,
} from '@vibesys/logs';
import {unprovided} from '@vibesys/machine';
import type {CoreState} from '@vibesys/run-state';
import {assign, raise, type SnapshotFrom, sendTo, setup} from 'xstate';

/** One startup stream as the panel lists it. */
export interface StartupStream {
  readonly id: string;
  /** The startup stage that wrote it (`building_image`, `starting_container`). */
  readonly stage: string;
  /** How the stream closed; null while it is open. */
  readonly outcome: LogStream['outcome'];
}

/** What the run says about the panel: whether it shows, and the stage that refreshes the list. */
export interface StartupLogObservation {
  readonly visible: boolean;
  readonly stage: string | null;
}

/**
 * The panel shows while the run is starting, and stays after the run failed while starting (it
 * failed with a startup stage recorded and before any round), so the output that explains the
 * failure stays on screen.
 */
export function startupLogObservation(run: CoreState): StartupLogObservation {
  const stage = run.startup?.stage ?? null;
  const failedStarting = run.status === 'failed' && stage !== null && run.rounds.length === 0;
  return {visible: run.status === 'starting' || failedStarting, stage};
}

export type StartupLogEvent =
  | ({readonly type: 'observe'} & StartupLogObservation)
  | {readonly type: 'expand'}
  | {readonly type: 'collapse'}
  | {readonly type: 'select'; readonly streamId: string};

type InternalEvent = {readonly type: 'refresh'} | {readonly type: 'retarget'};

export interface StartupLogInput {
  /** How long a failed listing waits before it asks again. */
  readonly retryMs?: number;
  readonly follow?: LogFollowConfig;
}

export interface StartupLogContext {
  readonly stage: string | null;
  /** The instance the last listing named; reads name it too. */
  readonly instanceId: string | null;
  readonly streams: readonly StartupStream[];
  /** The stream the user picked; null follows the newest. */
  readonly chosen: string | null;
  /** The stream the follower reads. */
  readonly followed: string | null;
  readonly listFailure: LogReadFailure | null;
  readonly retryMs: number;
  readonly follow: LogFollowConfig | undefined;
}

const DEFAULT_RETRY_MS = 2000;

function startupStreams(streams: readonly LogStream[]): StartupStream[] {
  return streams.flatMap(stream => {
    const {kind, stage} = stream.owner as {readonly kind?: unknown; readonly stage?: unknown};
    if (kind !== 'startup_step' || typeof stage !== 'string') return [];
    return [{id: stream.id, stage, outcome: stream.outcome}];
  });
}

/** The stream on screen: the user's pick while it is listed, else the newest. */
export function shownStream(context: StartupLogContext): string | null {
  const {streams, chosen} = context;
  if (chosen !== null && streams.some(stream => stream.id === chosen)) return chosen;
  return streams.at(-1)?.id ?? null;
}

function listed(event: unknown): LogListOutcome | undefined {
  return (event as {output?: LogListOutcome}).output;
}

export const startupLogMachine = setup({
  types: {
    context: {} as StartupLogContext,
    events: {} as StartupLogEvent | InternalEvent,
    input: {} as StartupLogInput,
  },
  actors: {
    list: unprovided<LogListOutcome, void>('log list'),
    follow: logFollowMachine,
  },
  guards: {
    visible: (_, params: {visible: boolean}) => params.visible,
    invisible: (_, params: {visible: boolean}) => !params.visible,
    stageChanged: ({context}, params: {stage: string | null}) => params.stage !== context.stage,
    unsupported: ({event}) => {
      const output = listed(event);
      return output !== undefined && !output.ok && logsUnsupported(output.failure);
    },
    failed: ({event}) => listed(event)?.ok === false,
    hasShown: ({context}) => context.instanceId !== null && shownStream(context) !== null,
    moved: ({context}) => shownStream(context) !== context.followed,
  },
  actions: {
    setStage: assign((_, params: {stage: string | null}) => ({stage: params.stage})),
    listed: assign(({event}) => {
      const output = listed(event);
      if (output === undefined || !output.ok) return {};
      return {
        instanceId: output.instanceId,
        streams: startupStreams(output.streams),
        listFailure: null,
      };
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
    refresh: raise({type: 'refresh'}),
    retarget: raise({type: 'retarget'}),
  },
  delays: {
    retryList: ({context}) => context.retryMs,
  },
}).createMachine({
  id: 'startupLog',
  context: ({input}) => ({
    stage: null,
    instanceId: null,
    streams: [],
    chosen: null,
    followed: null,
    listFailure: null,
    retryMs: input.retryMs ?? DEFAULT_RETRY_MS,
    follow: input.follow,
  }),
  initial: 'hidden',
  states: {
    hidden: {
      on: {
        observe: {
          guard: {type: 'visible', params: ({event}) => ({visible: event.visible})},
          target: 'shown',
          actions: {type: 'setStage', params: ({event}) => ({stage: event.stage})},
        },
      },
    },
    shown: {
      initial: 'expanded',
      on: {
        observe: [
          {
            guard: {type: 'invisible', params: ({event}) => ({visible: event.visible})},
            target: 'hidden',
          },
          {
            guard: {type: 'stageChanged', params: ({event}) => ({stage: event.stage})},
            actions: [{type: 'setStage', params: ({event}) => ({stage: event.stage})}, 'refresh'],
          },
        ],
      },
      states: {
        collapsed: {on: {expand: 'expanded'}},
        expanded: {
          type: 'parallel',
          on: {
            collapse: 'collapsed',
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
                  on: {refresh: {target: 'loading', reenter: true}},
                  invoke: {
                    id: 'list',
                    src: 'list',
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
                      instanceId: context.instanceId ?? '',
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
    },
  },
});

export type StartupLogSnapshot = SnapshotFrom<typeof startupLogMachine>;

/** What the panel renders. */
export interface StartupLogView {
  readonly visible: boolean;
  readonly expanded: boolean;
  /** The listing's state; `unsupported` when the host offers no logs. */
  readonly listing: 'loading' | 'loaded' | 'failed' | 'unsupported';
  readonly listFailure: LogReadFailure | null;
  readonly streams: readonly StartupStream[];
  readonly shown: string | null;
  /** The shown stream's lines, its unfinished last line included. */
  readonly lines: readonly string[];
  /** Earlier output of the shown stream exists that the view does not hold. */
  readonly earlier: boolean;
  /** The shown stream's last read failure; cleared by its next chunk. */
  readonly readFailure: LogReadFailure | null;
}

/** The follower's part of the view; the shell reads it from the invoked follower's snapshot. */
export interface FollowerView {
  readonly view: LogView;
  readonly failure: LogReadFailure | null;
}

function listingOf(snapshot: StartupLogSnapshot): StartupLogView['listing'] {
  for (const state of ['unsupported', 'failed', 'loaded'] as const) {
    if (snapshot.matches({shown: {expanded: {list: state}}})) return state;
  }
  return 'loading';
}

/** The panel's view of `snapshot`, with the follower's state when one runs. */
export function startupLogView(
  snapshot: StartupLogSnapshot,
  follower: FollowerView | null,
): StartupLogView {
  const view = follower?.view ?? null;
  return {
    visible: snapshot.matches('shown'),
    expanded: snapshot.matches({shown: 'expanded'}),
    listing: listingOf(snapshot),
    listFailure: snapshot.context.listFailure,
    streams: snapshot.context.streams,
    shown: snapshot.context.followed ?? shownStream(snapshot.context),
    lines: view === null ? [] : logViewLines(view),
    earlier: view !== null && (view.evictedLines > 0 || (view.start ?? 0) > 0),
    readFailure: follower?.failure ?? null,
  };
}

/** The panel's words for a stage. */
function startupStageLabel(stage: string): string {
  switch (stage) {
    case 'building_image':
      return 'Building image';
    case 'starting_container':
      return 'Starting container';
    default:
      return stage.replaceAll('_', ' ');
  }
}

/** The panel's words for one stream in its picker. */
export function startupStreamLabel(stream: StartupStream): string {
  const stage = startupStageLabel(stream.stage);
  return stream.outcome === null ? `${stage} (running)` : `${stage} (${stream.outcome})`;
}

/** What the panel body says instead of, or above, the lines; null when there is nothing to say. */
export function startupLogNotice(view: StartupLogView): string | null {
  switch (view.listing) {
    case 'unsupported':
      return "This host's VibeSys does not serve logs; update it there to see startup output.";
    case 'failed':
      return `Could not list the startup logs (${view.listFailure?.message ?? 'unknown error'}); retrying.`;
    case 'loading':
      if (view.shown === null) return 'Looking for startup output…';
      break;
    case 'loaded':
      if (view.shown === null) return 'No startup output has been recorded yet.';
      break;
  }
  if (view.readFailure !== null) return `Could not read this log: ${view.readFailure.message}`;
  return null;
}
