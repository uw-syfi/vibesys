/**
 * The run level's panes (the performance row above them is `Hero`, drawn by the screen at every
 * level): the left one switches between Ideas, Timeline, Agents and Logs (`state.zoom` mode), the
 * right one is Now, what runs at this moment, each row a shortcut to its live step.
 */

import type {CoreState} from '@vibesys/run-state';
import {
  EmptyState,
  markdownToPlain,
  Pane,
  Panes,
  PickList,
  PickRow,
  SegmentedControl,
  Stack,
  Text,
} from '@vibesys/ui';
import {type JSX, useState} from 'react';
import {
  ageWords,
  type IdeasFilter,
  type IdeasSort,
  type ImplementerOutcomes,
  ideasView,
  NO_MODEL_NAMES,
  type NowItem,
  nowView,
  predatesTraceWords,
  type RunMode,
  type RunPageAction,
  type RunPageState,
  roundsBeforeTrace,
  traceArrival,
  usageContext,
  usageRecords,
  type ZoomEvent,
} from '../../state/index.js';
import {FramedPane} from './framed-pane.js';
import {IdeaPickRow} from './idea-pick-row.js';
import {IdeasTable} from './ideas-table.js';
import {AgentsView, type AgentsViewProps} from './views/agents/agents-view.js';
import {agentsHeader, logsHeader, timelineHeader} from './views/headers.js';
import {LogsView, type LogsViewProps} from './views/logs/logs-view.js';
import type {PaneHeader, ViewBase, Zoom} from './views/props.js';
import {TimelineView, type TimelineViewProps} from './views/timeline/timeline-view.js';
import {UsageView} from './views/usage/usage-view.js';

type Dispatch = (action: RunPageAction) => void;

const MODES: readonly {readonly value: RunMode; readonly label: string}[] = [
  {value: 'ideas', label: 'Ideas'},
  {value: 'timeline', label: 'Timeline'},
  {value: 'agents', label: 'Agents'},
  {value: 'usage', label: 'Usage'},
  {value: 'logs', label: 'Logs'},
];

export function ModeSwitch(props: {
  readonly mode: RunMode;
  readonly options: readonly {readonly value: RunMode; readonly label: string}[];
  readonly zoom: Zoom;
}): JSX.Element {
  return (
    <SegmentedControl
      label="Show"
      size="xs"
      options={props.options}
      value={props.mode}
      change={mode => props.zoom({type: 'mode', mode})}
    />
  );
}

/** The ideas as a flat list: full at the run level, compact beside an opened idea. */
export function IdeasList(props: {
  readonly base: ViewBase;
  readonly outcomes: ImplementerOutcomes;
  readonly filter: IdeasFilter;
  readonly selected: string | null;
  readonly compact: boolean;
  readonly dispatch: Dispatch;
}): JSX.Element {
  const {base, filter} = props;
  const view = ideasView(base.model, filter);
  if (view.rows.length === 0) return <Text tone="muted">No matches</Text>;
  return (
    <Stack gap="space-2" grow>
      <PickList
        label="Ideas"
        size="fill"
        {...(props.compact
          ? {}
          : {
              header: {
                start: 'idea',
                end: [
                  {label: 'vs best', bar: true},
                  {label: 'vs input', fixed: 'figure' as const},
                  {label: 'from', fixed: 'trail' as const},
                ],
              },
            })}
        {...(props.selected === null ? {} : {selected: props.selected})}
      >
        {view.rows.map(row => (
          <IdeaPickRow
            key={row.id}
            row={row}
            outcomes={props.outcomes}
            compact={props.compact}
            now={base.clock.now}
            zoom={base.zoom}
          />
        ))}
      </PickList>
    </Stack>
  );
}

function IdeasFace(props: {
  readonly state: RunPageState;
  readonly base: ViewBase;
  readonly selected: string | null;
  readonly modeControls: boolean;
  readonly dispatch: Dispatch;
}): JSX.Element {
  const {state, base, dispatch} = props;
  // The order the reader chose by a column header; null: running first, then the first objective's best.
  const [sort, setSort] = useState<IdeasSort | null>(null);
  const modes = props.modeControls ? (
    <ModeSwitch mode="ideas" options={MODES} zoom={base.zoom} />
  ) : null;
  if (base.model.candidates.candidates.length === 0) return <>{modes}</>;
  return (
    <>
      {modes}
      <IdeasTable
        base={base}
        filter={state.ideas}
        sort={sort}
        setSort={setSort}
        selected={props.selected}
        dispatch={dispatch}
      />
    </>
  );
}

/** The Ideas master pane is the same component and width before and after an idea opens. */
export function IdeasPane(props: {
  readonly state: RunPageState;
  readonly base: ViewBase;
  readonly core: CoreState | null;
  readonly selected: string | null;
  readonly dispatch: Dispatch;
}): JSX.Element {
  return (
    <FramedPane
      kicker="ideas"
      title={`${props.base.model.candidates.candidates.length}`}
      width="master"
      header={{}}
      fixed
    >
      <IdeasFace {...props} modeControls />
    </FramedPane>
  );
}

/** Where a Now row jumps: its live step, its idea, or its agent. */
function nowZoom(item: NowItem): ZoomEvent {
  switch (item.open.kind) {
    case 'step':
      return {type: 'open-step', idea: item.open.idea, step: item.open.step};
    case 'idea':
      return {type: 'open-idea', idea: item.open.idea};
    case 'agent':
      return {type: 'open-agent', agent: item.open.agent};
  }
}

export function NowPane(props: {readonly base: ViewBase}): JSX.Element | null {
  const {base} = props;
  const items = nowView(base.model);
  if (items.length === 0) return null;
  return (
    <Pane kicker="now" title={`${items.length}`} width="list">
      <PickList label="Running now" size="fill">
        {items.map(item => (
          <PickRow
            key={item.id}
            id={item.id}
            title={markdownToPlain(item.context)}
            mono
            stacked
            summary={item.doing}
            figure={ageWords(item.since, base.clock.now)}
            figureTone="muted"
            state={{tone: 'live', label: 'running'}}
            select={() => base.zoom(nowZoom(item))}
          />
        ))}
      </PickList>
    </Pane>
  );
}

/** The Agents list's unfolded groups, kept by the page so every list of agents shares them. */
export function agentFolds(
  state: RunPageState,
  dispatch: Dispatch,
): Pick<AgentsViewProps, 'unfolded' | 'unfold'> {
  return {
    unfolded: state.agentsOpen,
    unfold: id => dispatch({type: 'agents-open', id, open: true}),
  };
}

/** The run level's face for `mode`: its body and what its view adds to the pane's header. */
function runFace(
  state: RunPageState,
  base: ViewBase,
  mode: RunMode,
  dispatch: Dispatch,
): {readonly header: PaneHeader; readonly body: JSX.Element} {
  if (mode !== 'logs' && traceArrival(state) === 'awaited') {
    // Until the run's spans arrive, a face would read as empty: say they are on their way.
    return {header: {}, body: <EmptyState title="Reading the run…" />};
  }
  const before = roundsBeforeTrace(state);
  if (mode !== 'logs' && before !== null) {
    const words = predatesTraceWords(before);
    return {header: {}, body: <EmptyState title={words.title} body={words.body} />};
  }
  switch (mode) {
    case 'timeline': {
      const view: TimelineViewProps = {...base, idea: null};
      return {header: timelineHeader(view), body: <TimelineView {...view} />};
    }
    case 'agents': {
      const view: AgentsViewProps = {
        ...base,
        ...agentFolds(state, dispatch),
        selected: null,
        compact: false,
      };
      return {header: agentsHeader(view), body: <AgentsView {...view} />};
    }
    case 'usage':
      return {
        header: {},
        body:
          state.progress && state.progress.phase !== 'complete' ? (
            <EmptyState title="Catching up" body="Reading the run’s committed usage" />
          ) : (
            <UsageView
              emptyLabelsOnly={base.emptyLabelsOnly === true}
              records={usageRecords(state)}
              context={usageContext(base.model, base.names ?? NO_MODEL_NAMES)}
              controls={state.usage}
              change={controls => dispatch({type: 'usage-controls', controls})}
            />
          ),
      };
    case 'logs': {
      const view: LogsViewProps = {...base, state, scope: {kind: 'run'}, dispatch};
      return {header: logsHeader(view), body: <LogsView {...view} />};
    }
    case 'ideas':
      return {
        header: {},
        body: (
          <IdeasFace
            state={state}
            base={base}
            selected={null}
            modeControls={false}
            dispatch={dispatch}
          />
        ),
      };
  }
}

const KICKERS: Readonly<Record<RunMode, string>> = {
  ideas: 'ideas',
  timeline: 'timeline',
  agents: 'agents',
  usage: 'usage',
  logs: 'logs',
};

/** The run level: the chosen face and Now. */
export function RunLevel(props: {
  readonly state: RunPageState;
  readonly base: ViewBase;
  readonly core: CoreState | null;
  readonly mode: RunMode;
  readonly dispatch: Dispatch;
}): JSX.Element {
  const {state, base, mode, dispatch} = props;
  const total = {
    agents: base.model.agents.agents.length,
    usage: usageRecords(state).length,
    ideas: base.model.candidates.candidates.length,
    timeline: base.model.candidates.candidates.length,
    logs: 0,
  }[mode];
  const face = runFace(state, base, mode, dispatch);
  return (
    <Panes>
      <FramedPane
        kicker={KICKERS[mode]}
        title={mode === 'logs' ? 'run' : `${total}`}
        width="fill"
        header={face.header}
      >
        <ModeSwitch mode={mode} options={MODES} zoom={base.zoom} />
        {face.body}
      </FramedPane>
      {mode === 'usage' ? null : <NowPane base={base} />}
    </Panes>
  );
}
