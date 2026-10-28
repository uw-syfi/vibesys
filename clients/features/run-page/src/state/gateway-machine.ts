/** Gateway screen control owns the loader, content request and selected log follower. */
import {emptyLogView, foldChunk, type LogView} from '@vibesys/logs';
import {unprovided} from '@vibesys/machine';
import type {
  LoadProgress,
  RunLivenessHint,
  SkeletonLoad,
  SkeletonLoadFailure,
} from '@vibesys/run-skeleton';
import {type RecordRow, recordRows} from '@vibesys/run-state/records';
import type {SkeletonContent} from '@vibesys/run-state/skeleton';
import {
  emptyNativeTranscript,
  type NativeTranscript,
  nativeTranscriptHashes,
} from '@vibesys/run-state/transcript';
import {assign, fromCallback, setup} from 'xstate';
import {
  type RunPageAction,
  type RunPageSeed,
  type RunPageState,
  runPageInitial,
  runPageStep,
} from './run-page.js';

export interface GatewayInput {
  readonly seed: RunPageSeed;
}
export interface GatewayLogInput {
  readonly runId: string;
  readonly streamId: string;
  readonly receive: (event: GatewayEvent) => void;
}
export interface GatewayTranscriptInput {
  readonly runId: string;
  readonly spanId: string;
  readonly streamId: string;
  readonly receive: (event: GatewayEvent) => void;
}
export type GatewayEvent =
  | {type: 'refresh-writer'}
  | {type: 'follow-transcript'; selection: {spanId: string; streamId: string} | null}
  | {type: 'follow-diffs'; hashes: readonly string[]}
  | {type: 'transcript'; spanId: string; transcript: NativeTranscript}
  | {type: 'ui'; action: RunPageAction}
  | {type: 'log'; streamId: string; text: string; offset: number; next: number}
  | {type: 'log-failed'}
  | {type: 'records'; rows: readonly RecordRow[]}
  | {type: 'retry'}
  | {
      type: 'loaded';
      load: SkeletonLoad | undefined;
      progress: LoadProgress;
      failure: SkeletonLoadFailure | undefined;
      terminal: boolean;
      hint?: RunLivenessHint;
      fresh: boolean;
    };
export interface GatewayRunContext extends GatewayInput {
  load: SkeletonLoad | undefined;
  progress: LoadProgress;
  failure: SkeletonLoadFailure | undefined;
  terminal: boolean;
  hint: RunLivenessHint | undefined;
  /** The reader's liveness lease; the controls read this same fact. */
  fresh: boolean;
  head: RunLivenessHint['head'];
  ui: RunPageState;
  text: Readonly<Record<string, string>>;
  contentHash: string;
  attemptedContent: ReadonlySet<string>;
  replies: NonNullable<SkeletonContent['replies']>;
  error: string | null;
  selectedTranscript: {spanId: string; streamId: string} | null;
  transcripts: Readonly<Record<string, NativeTranscript>>;
  /** The diff blobs of the opened idea's attempts: read on demand, never for every idea. */
  followedDiffs: readonly string[];
}

function nextContentHash(context: GatewayRunContext): string {
  const epochs = context.load?.skeleton.epochs ?? [];
  for (let index = epochs.length - 1; index >= 0; index -= 1) {
    const hash = epochs[index]?.epoch?.run?.objective?.sha256 ?? '';
    if (hash !== '' && context.text[hash] === undefined && !context.attemptedContent.has(hash))
      return hash;
  }
  return (
    [
      ...context.followedDiffs,
      ...Object.values(context.transcripts).flatMap(nativeTranscriptHashes),
      ...Object.values(context.replies)
        .flat()
        .map(ref => ref.hash),
    ]
      .map(hash => ({hash}))
      .map(ref => ref.hash)
      .find(hash => context.text[hash] === undefined && !context.attemptedContent.has(hash)) ?? ''
  );
}

function replyAttachments(
  previous: GatewayRunContext['replies'],
  rows: readonly RecordRow[],
): GatewayRunContext['replies'] {
  const replies = {...previous};
  for (const row of rows) {
    const attachment = row.attachment;
    if (attachment?.role !== 'reply' || !attachment.blob) continue;
    replies[attachment.span] = [
      ...(replies[attachment.span] ?? []),
      {seq: BigInt(row.seq), hash: attachment.blob.sha256},
    ];
  }
  return replies;
}

/** Presentation changes reuse the existing pure reducer; gateway commands never reach legacy ports. */
function uiStep(state: RunPageState, action: RunPageAction): RunPageState {
  switch (action.type) {
    case 'view':
    case 'ideas-filter':
    case 'usage-controls':
    case 'ideas-layout':
    case 'ideas-open':
    case 'ideas-pin':
    case 'ideas-hover':
    case 'agents-open':
    case 'select-stream':
    case 'view-logs':
      return runPageStep(state, action).state;
    default:
      return state;
  }
}

function receivedLog(
  context: GatewayRunContext,
  event: Extract<GatewayEvent, {type: 'log'}>,
): Readonly<Record<string, LogView>> {
  const previous =
    (Object.hasOwn(context.ui.views, event.streamId)
      ? context.ui.views[event.streamId]
      : undefined) ?? emptyLogView();
  return {
    ...context.ui.views,
    [event.streamId]: foldChunk(previous, {
      offset: event.offset,
      next_offset: event.next,
      data: event.text,
      truncated_before: false,
      eof: false,
      closed: false,
      pipes: [],
    }),
  };
}

/** Committed headlines survive a lost lease independently of the transient lock hint. */
function receivedHead(previous: RunLivenessHint['head'], next: RunLivenessHint['head']) {
  if (!previous) return next;
  if (!next) return previous;
  return next.epoch > previous.epoch ||
    (next.epoch === previous.epoch && (next.committedSeq ?? -1n) >= (previous.committedSeq ?? -1n))
    ? next
    : previous;
}

export const gatewayRunMachine = setup({
  types: {context: {} as GatewayRunContext, input: {} as GatewayInput, events: {} as GatewayEvent},
  actors: {
    loader: fromCallback<GatewayEvent, {runId: string}>(() => {
      throw new Error('loader not provided');
    }),
    content: unprovided<{hash: string; text: string}, {runId: string; hash: string}>(
      'content by hash',
    ),
    log: unprovided<void, GatewayLogInput>('log transport'),
    transcript: unprovided<void, GatewayTranscriptInput>('transcript transport'),
  },
  guards: {
    needsContent: ({context}) => nextContentHash(context) !== '',
  },
  actions: {
    receivedRecords: assign(({context, event}) => {
      if (event.type !== 'records') return {};
      const rows = recordRows(context.ui.recordLog ?? [], event.rows);
      return {
        replies: replyAttachments(context.replies, event.rows),
        ui: {...context.ui, recordLog: rows, recordLogError: null},
      };
    }),
  },
}).createMachine({
  id: 'gatewayRun',
  context: ({input}) => ({
    ...input,
    load: undefined,
    progress: {phase: 'skeleton'},
    failure: undefined,
    terminal: false,
    hint: undefined,
    fresh: false,
    head: undefined,
    ui: runPageInitial(input.seed),
    text: {},
    contentHash: '',
    attemptedContent: new Set(),
    replies: {},
    error: null,
    selectedTranscript: null,
    transcripts: {},
    followedDiffs: [],
  }),
  initial: 'active',
  states: {
    active: {
      entry: assign({
        load: undefined,
        progress: {phase: 'skeleton'},
        failure: undefined,
        terminal: false,
        hint: undefined,
        fresh: false,
        head: undefined,
        contentHash: '',
        attemptedContent: new Set(),
        replies: {},
        ui: ({context}) => {
          const {recordLog: _rows, recordLogError: _error, ...rest} = context.ui;
          return {...rest, selected: null, views: {}};
        },
      }),
      type: 'parallel',
      on: {
        loaded: {
          actions: assign({
            load: ({context, event}) =>
              (event.load?.skeleton.throughSeq ?? -1n) >= (context.load?.skeleton.throughSeq ?? -1n)
                ? event.load
                : context.load,
            progress: ({event}) => event.progress,
            failure: ({event}) => event.failure,
            terminal: ({event}) => event.terminal,
            hint: ({event}) => event.hint,
            fresh: ({event}) => event.fresh,
            head: ({context, event}) => receivedHead(context.head, event.hint?.head),
          }),
        },
        records: {actions: 'receivedRecords'},
        'follow-diffs': {actions: assign({followedDiffs: ({event}) => event.hashes})},
        retry: {target: '#gatewayRun.active', reenter: true},
        ui: {actions: assign({ui: ({context, event}) => uiStep(context.ui, event.action)})},
      },
      states: {
        writer: {
          initial: 'following',
          on: {'refresh-writer': {target: '.following', reenter: true}},
          states: {
            following: {
              invoke: {
                id: 'loader',
                src: 'loader',
                input: ({context}) => ({runId: context.seed.identity.runId}),
                onError: {actions: assign({error: 'Unavailable'})},
              },
            },
          },
        },
        content: {
          initial: 'idle',
          states: {
            idle: {
              always: {
                guard: 'needsContent',
                target: 'reading',
                actions: assign(({context}) => {
                  const hash = nextContentHash(context);
                  return {
                    contentHash: hash,
                    attemptedContent: new Set([...context.attemptedContent, hash]),
                  };
                }),
              },
            },
            reading: {
              invoke: {
                id: 'content',
                src: 'content',
                input: ({context}) => ({
                  runId: context.seed.identity.runId,
                  hash: context.contentHash,
                }),
                onDone: {
                  target: 'idle',
                  actions: assign({
                    text: ({context, event}) => ({
                      ...context.text,
                      [event.output.hash]: event.output.text,
                    }),
                  }),
                },
                onError: {target: 'idle', actions: assign({error: 'Content unavailable'})},
              },
              after: {30000: {target: 'idle', actions: assign({error: 'Content timeout'})}},
            },
          },
        },
        transcript: {
          initial: 'idle',
          on: {
            'follow-transcript': {
              guard: ({context, event}) =>
                event.selection?.streamId !== context.selectedTranscript?.streamId,
              target: '.choose',
              actions: assign(({context, event}) => ({
                selectedTranscript: event.selection,
                transcripts: event.selection
                  ? {...context.transcripts, [event.selection.spanId]: emptyNativeTranscript()}
                  : context.transcripts,
              })),
            },
          },
          states: {
            idle: {},
            choose: {
              always: [
                {guard: ({context}) => context.selectedTranscript !== null, target: 'reading'},
                {target: 'idle'},
              ],
            },
            reading: {
              invoke: {
                id: 'transcript',
                src: 'transcript',
                input: ({context, self}) => ({
                  runId: context.seed.identity.runId,
                  spanId: context.selectedTranscript?.spanId ?? '',
                  streamId: context.selectedTranscript?.streamId ?? '',
                  receive: event => self.send(event),
                }),
                onError: {target: 'idle', actions: assign({error: 'Transcript unavailable'})},
                onDone: {target: 'idle'},
              },
              initial: 'waiting',
              states: {
                waiting: {
                  after: {
                    30000: {
                      target: '#gatewayRun.active.transcript.idle',
                      actions: assign({error: 'Transcript timeout'}),
                    },
                  },
                  on: {
                    transcript: {
                      guard: ({event}) => event.transcript.entries.length > 0,
                      target: 'live',
                      actions: assign(({context, event}) => ({
                        transcripts: {
                          ...context.transcripts,
                          [event.spanId]: event.transcript,
                        },
                      })),
                    },
                  },
                },
                live: {},
              },
              on: {
                transcript: {
                  actions: assign(({context, event}) => ({
                    transcripts: {
                      ...context.transcripts,
                      [event.spanId]: event.transcript,
                    },
                  })),
                },
              },
            },
          },
        },
        logs: {
          initial: 'idle',
          on: {
            ui: [
              {
                guard: ({event, context}) =>
                  event.action.type === 'select-stream' &&
                  (event.action.streamId !== context.ui.selected ||
                    context.ui.logProblem !== undefined),
                target: '.following',
                actions: assign({
                  ui: ({context, event}) => ({
                    ...uiStep(context.ui, event.action),
                    logProblem: undefined,
                  }),
                }),
              },
              {actions: assign({ui: ({context, event}) => uiStep(context.ui, event.action)})},
            ],
          },
          states: {
            idle: {},
            following: {
              invoke: {
                id: 'log',
                src: 'log',
                input: ({context, self}) => ({
                  runId: context.seed.identity.runId,
                  streamId: context.ui.selected ?? '',
                  receive: event => self.send(event),
                }),
                onError: {
                  target: 'idle',
                  actions: assign({
                    ui: ({context}) => ({
                      ...context.ui,
                      logProblem: {streamId: context.ui.selected ?? '', message: 'Log unavailable'},
                    }),
                  }),
                },
              },
              on: {
                log: {
                  actions: assign({
                    ui: ({context, event}) => ({
                      ...context.ui,
                      views: receivedLog(context, event),
                      logProblem: undefined,
                    }),
                  }),
                },
                'log-failed': {
                  target: 'idle',
                  actions: assign({
                    ui: ({context}) => ({
                      ...context.ui,
                      logProblem: {streamId: context.ui.selected ?? '', message: 'Log unavailable'},
                    }),
                  }),
                },
              },
            },
          },
        },
      },
    },
  },
});
