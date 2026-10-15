/** Campaign navigation and playback: the machine owns loading, streaming, and its playback delay. */
import {unprovided} from '@vibesys/machine';
import {assign, fromCallback, setup} from 'xstate';
import {
  campaignFoldErrorMessage,
  type FoldState,
  foldCampaignFrame,
  foldStateToRecord,
  initialFoldState,
  latestMoment,
} from './campaign-fold.js';
import type {CampaignFrame} from './campaign-frames.js';
import type {CampaignRecord} from './campaign-record.js';
import type {
  CampaignTimeline,
  CampaignView,
  WorkstreamLayout,
  WorkstreamSort,
} from './campaign-view.js';

export interface CampaignUi {
  readonly metricId: string;
  readonly pointIndex: number;
  readonly following: boolean;
  readonly playing: boolean;
  readonly selectedWorkstreamId: string | null;
  readonly selectedAgentId: string | null;
  readonly view: CampaignView;
  readonly timelineMode: CampaignTimeline;
  readonly workstreamLayout: WorkstreamLayout;
  readonly workstreamSort: WorkstreamSort;
}

export interface CampaignContext {
  readonly mode: 'history' | 'live' | 'replay';
  readonly record: CampaignRecord | null;
  readonly fold: FoldState;
  readonly ui: CampaignUi;
  readonly error: string | null;
  readonly loading: boolean;
}

export type CampaignAction =
  | {readonly type: 'load'}
  | {readonly type: 'close'}
  | {readonly type: 'frame'; readonly frame: CampaignFrame}
  | {readonly type: 'stream-error'; readonly error: string}
  | {readonly type: 'metric'; readonly id: string}
  | {readonly type: 'point'; readonly index: number}
  | {readonly type: 'play'; readonly playing: boolean}
  | {readonly type: 'workstream'; readonly id: string | null}
  | {readonly type: 'agent'; readonly id: string | null}
  | {readonly type: 'view'; readonly view: CampaignView}
  | {readonly type: 'timeline'; readonly mode: CampaignTimeline}
  | {readonly type: 'layout'; readonly layout: WorkstreamLayout}
  | {readonly type: 'sort'; readonly sort: WorkstreamSort};

export interface CampaignInput {
  readonly mode: CampaignContext['mode'];
  readonly record?: CampaignRecord;
}

export function campaignRecord(context: CampaignContext): CampaignRecord | null {
  return context.mode === 'live'
    ? foldStateToRecord(context.fold, latestMoment(context.fold))
    : context.record;
}

export function orderedMeasurements(record: CampaignRecord | null): CampaignRecord['measurements'] {
  return [...(record?.measurements ?? [])].sort((a, b) => a.sequence - b.sequence);
}

export function defaultMetricId(record: CampaignRecord | null): string {
  return (
    record?.objective.target?.metricId ??
    record?.objective.metrics[0]?.id ??
    orderedMeasurements(record)[0]?.values[0]?.metricId ??
    ''
  );
}

export function initialCampaignUi(record: CampaignRecord | null): CampaignUi {
  const measurements = orderedMeasurements(record);
  const metricId = defaultMetricId(record);
  return {
    metricId,
    pointIndex: latestMetricIndex(measurements, metricId),
    following: true,
    playing: false,
    selectedWorkstreamId: null,
    selectedAgentId: null,
    view: 'dashboard',
    timelineMode: 'workstreams',
    workstreamLayout: 'kanban',
    workstreamSort: 'start-asc',
  };
}

function latestMetricIndex(measurements: CampaignRecord['measurements'], metricId: string): number {
  for (let index = measurements.length - 1; index >= 0; index--)
    if (measurements[index]?.values.some(value => value.metricId === metricId)) return index;
  return 0;
}

function tailIndex(context: CampaignContext): number {
  return Math.max(0, (campaignRecord(context)?.measurements.length ?? 0) - 1);
}

function navigate(context: CampaignContext, event: CampaignAction): CampaignUi {
  const ui = context.ui;
  switch (event.type) {
    case 'metric':
      return {
        ...ui,
        metricId: event.id,
        ...(context.mode === 'live'
          ? {}
          : {
              playing: false,
              pointIndex: latestMetricIndex(orderedMeasurements(context.record), event.id),
            }),
      };
    case 'point': {
      const index = Math.max(0, Math.min(tailIndex(context), event.index));
      return {...ui, pointIndex: index, playing: false, following: index >= tailIndex(context)};
    }
    case 'play':
      return {
        ...ui,
        playing: event.playing,
        following: event.playing,
        pointIndex: context.mode === 'live' && ui.following ? tailIndex(context) : ui.pointIndex,
      };
    case 'workstream':
      return {...ui, selectedWorkstreamId: event.id};
    case 'agent':
      return {...ui, selectedAgentId: event.id};
    case 'view':
      return {...ui, view: event.view};
    case 'timeline':
      return {...ui, timelineMode: event.mode};
    case 'layout':
      return {...ui, workstreamLayout: event.layout};
    case 'sort':
      return {...ui, workstreamSort: event.sort};
    default:
      return ui;
  }
}

export const campaignMachine = setup({
  types: {
    context: {} as CampaignContext,
    events: {} as CampaignAction,
    input: {} as CampaignInput,
  },
  actors: {
    load: unprovided<CampaignRecord>('campaign load'),
    stream: fromCallback<CampaignAction>(() => {
      throw new Error('campaign stream was not provided');
    }),
  },
  guards: {
    live: ({context}) => context.mode === 'live',
    hasRecord: ({context}) => context.record !== null,
    playing: ({event}) => event.type === 'play' && event.playing,
    atTail: ({context}) => context.ui.pointIndex >= tailIndex(context),
    frameRejected: ({context, event}) =>
      event.type === 'frame' && foldCampaignFrame(context.fold, event.frame).kind === 'rejected',
    frameCompletes: ({context, event}) => {
      if (event.type !== 'frame') return false;
      const outcome = foldCampaignFrame(context.fold, event.frame);
      return outcome.kind === 'applied' && outcome.state.status === 'completed';
    },
  },
  actions: {
    navigate: assign(({context, event}) => ({ui: navigate(context, event)})),
    pause: assign(({context}) => ({ui: {...context.ui, playing: false}})),
    tick: assign(({context}) => ({
      ui: {...context.ui, pointIndex: Math.min(tailIndex(context), context.ui.pointIndex + 1)},
    })),
    applyFrame: assign(({context, event}) => {
      if (event.type !== 'frame') return {};
      const outcome = foldCampaignFrame(context.fold, event.frame);
      return outcome.kind === 'applied' ? {fold: outcome.state, error: null} : {};
    }),
    rejectFrame: assign(({context, event}) => {
      if (event.type !== 'frame') return {};
      const outcome = foldCampaignFrame(context.fold, event.frame);
      return outcome.kind === 'rejected' ? {error: campaignFoldErrorMessage(outcome.error)} : {};
    }),
  },
}).createMachine({
  id: 'campaign',
  context: ({input}) => ({
    mode: input.mode,
    record: input.record ?? null,
    fold: initialFoldState(),
    ui: initialCampaignUi(input.record ?? null),
    error: null,
    loading: input.record === undefined && input.mode !== 'live',
  }),
  initial: 'route',
  on: {
    close: '.closed',
    workstream: {actions: 'navigate'},
    agent: {actions: 'navigate'},
    view: {actions: 'navigate'},
    timeline: {actions: 'navigate'},
    layout: {actions: 'navigate'},
    sort: {actions: 'navigate'},
  },
  states: {
    route: {
      always: [
        {guard: 'live', target: 'live'},
        {guard: 'hasRecord', target: 'history'},
        {target: 'idle'},
      ],
    },
    idle: {on: {load: 'loading'}},
    loading: {
      entry: assign({loading: true, error: null}),
      invoke: {
        id: 'load',
        src: 'load',
        onDone: {
          target: 'history',
          actions: assign(({event}) => ({
            record: event.output,
            ui: initialCampaignUi(event.output),
            loading: false,
          })),
        },
        onError: {
          target: 'failed',
          actions: assign(({event}) => ({
            error: event.error instanceof Error ? event.error.message : String(event.error),
            loading: false,
          })),
        },
      },
    },
    failed: {on: {load: 'loading'}},
    history: {
      initial: 'paused',
      on: {
        point: {target: '.paused', actions: 'navigate'},
        metric: {target: '.paused', actions: 'navigate'},
        play: [
          {guard: 'playing', target: '.playing', actions: 'navigate'},
          {target: '.paused', actions: 'navigate'},
        ],
      },
      states: {
        paused: {entry: 'pause'},
        playing: {
          always: {guard: 'atTail', target: 'paused'},
          after: {300: {target: 'playing', reenter: true, actions: 'tick'}},
        },
      },
    },
    live: {
      initial: 'streaming',
      on: {
        point: {actions: 'navigate'},
        metric: {actions: 'navigate'},
        play: {actions: 'navigate'},
      },
      states: {
        streaming: {
          invoke: {
            id: 'stream',
            src: 'stream',
            onError: {
              actions: assign(({event}) => ({
                error: event.error instanceof Error ? event.error.message : String(event.error),
              })),
            },
          },
          on: {
            frame: [
              {guard: 'frameRejected', target: 'integrityFailed', actions: 'rejectFrame'},
              {guard: 'frameCompletes', target: 'completed', actions: 'applyFrame'},
              {actions: 'applyFrame'},
            ],
            'stream-error': {actions: assign(({event}) => ({error: event.error}))},
          },
        },
        completed: {},
        integrityFailed: {},
      },
    },
    closed: {type: 'final', entry: assign({loading: false})},
  },
});
