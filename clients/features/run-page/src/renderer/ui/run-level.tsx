/**
 * The run level's panes (the performance row above them is `Hero`, drawn by the screen at every
 * level): the left one switches between Ideas, Timeline, Agents and Logs (`state.zoom` mode), the
 * right one is Now, what runs at this moment, each row a shortcut to its live step.
 */

import type {CoreState} from '@vibesys/run-state';
import {
  Button,
  DotLegend,
  EmptyState,
  Pane,
  Panes,
  PickList,
  PickRow,
  Row,
  SearchBox,
  SegmentedControl,
  Stack,
} from '@vibesys/ui';
import type {JSX} from 'react';
import {
  ageWords,
  bestLine,
  defaultLayout,
  type IdeasFilter,
  type IdeasLayout,
  type ImplementerOutcomes,
  ideasView,
  implementerOutcomes,
  type NowItem,
  nowView,
  type RunMode,
  type RunModel,
  type RunPageAction,
  type RunPageState,
  traceArrival,
  type ZoomEvent,
} from '../../state/index.js';
import {FramedPane} from './framed-pane.js';
import {IdeaPickRow} from './idea-pick-row.js';
import {BestLineRow, IdeasTreeList, LayoutSwitch} from './ideas-tree.js';
import {AgentsView, type AgentsViewProps} from './views/agents/agents-view.js';
import {agentsHeader, logsHeader, timelineHeader} from './views/headers.js';
import {LogsView, type LogsViewProps} from './views/logs/logs-view.js';
import type {PaneHeader, ViewBase, Zoom} from './views/props.js';
import {TimelineView, type TimelineViewProps} from './views/timeline/timeline-view.js';

type Dispatch = (action: RunPageAction) => void;

const MODES: readonly {readonly value: RunMode; readonly label: string}[] = [
  {value: 'ideas', label: 'Ideas'},
  {value: 'timeline', label: 'Timeline'},
  {value: 'agents', label: 'Agents'},
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
      size="sm"
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
  const {base, filter, dispatch} = props;
  const view = ideasView(base.model, filter);
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
      {view.hiddenFailed === 0 ? null : (
        <Button
          size="sm"
          variant="quiet"
          text={`+ ${view.hiddenFailed} failed`}
          press={() => dispatch({type: 'ideas-filter', filter: {...filter, failed: true}})}
        />
      )}
    </Stack>
  );
}

function IdeasLegend(props: {
  readonly state: RunPageState;
  readonly base: ViewBase;
  readonly dispatch: Dispatch;
}): JSX.Element {
  const filter = props.state.ideas;
  const {counts} = ideasView(props.base.model, filter);
  const toggle = (key: 'passed' | 'running' | 'failed') => (on: boolean) =>
    props.dispatch({type: 'ideas-filter', filter: {...filter, [key]: on}});
  return (
    <DotLegend
      items={[
        {
          tone: 'ok',
          label: `${counts.passed} passed`,
          on: filter.passed,
          toggle: toggle('passed'),
        },
        {
          tone: 'live',
          label: `${counts.running} running`,
          on: filter.running,
          toggle: toggle('running'),
        },
        {
          tone: 'err',
          label: `${counts.failed} failed`,
          on: filter.failed,
          toggle: toggle('failed'),
        },
      ]}
    />
  );
}

/** The layout the Ideas face is in: the reader's pick, else by the run. */
function ideasLayout(state: RunPageState, model: RunModel): IdeasLayout {
  return state.ideasLayout ?? defaultLayout(model);
}

function IdeasFace(props: {
  readonly state: RunPageState;
  readonly base: ViewBase;
  readonly outcomes: ImplementerOutcomes;
  readonly dispatch: Dispatch;
}): JSX.Element {
  const {state, base, outcomes, dispatch} = props;
  if (base.model.candidates.candidates.length === 0) {
    return <EmptyState title="No ideas yet" body="Each idea the run tries shows here." />;
  }
  const line = bestLine(base.model);
  const layout = ideasLayout(state, base.model);
  return (
    <>
      <IdeasLegend state={state} base={base} dispatch={dispatch} />
      {line === null ? null : <BestLineRow line={line} dispatch={dispatch} />}
      <Row gap="space-3" align="center">
        <Stack gap="space-0" grow>
          <SearchBox
            label="Search ideas"
            placeholder="Search ideas"
            value={state.ideas.query}
            change={query => dispatch({type: 'ideas-filter', filter: {...state.ideas, query}})}
          />
        </Stack>
        <LayoutSwitch layout={layout} dispatch={dispatch} />
      </Row>
      {layout === 'tree' ? (
        <IdeasTreeList base={base} outcomes={outcomes} state={state} dispatch={dispatch} />
      ) : (
        <IdeasList
          base={base}
          outcomes={outcomes}
          filter={state.ideas}
          selected={null}
          compact={false}
          dispatch={dispatch}
        />
      )}
    </>
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

function NowPane(props: {readonly base: ViewBase}): JSX.Element | null {
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
            title={item.doing}
            mono
            summary={item.context}
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
  outcomes: ImplementerOutcomes,
  mode: RunMode,
  dispatch: Dispatch,
): {readonly header: PaneHeader; readonly body: JSX.Element} {
  if (mode !== 'logs' && traceArrival(state) === 'awaited') {
    // Until the run's spans arrive, a face would read as empty: say they are on their way.
    return {header: {}, body: <EmptyState title="Reading the run…" />};
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
    case 'logs': {
      const view: LogsViewProps = {...base, state, scope: {kind: 'run'}, dispatch};
      return {header: logsHeader(view), body: <LogsView {...view} />};
    }
    case 'ideas':
      return {
        header: {},
        body: <IdeasFace state={state} base={base} outcomes={outcomes} dispatch={dispatch} />,
      };
  }
}

const KICKERS: Readonly<Record<RunMode, string>> = {
  ideas: 'ideas',
  timeline: 'timeline',
  agents: 'agents',
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
  const total =
    mode === 'agents' ? base.model.agents.agents.length : base.model.candidates.candidates.length;
  const outcomes = implementerOutcomes(props.core, base.model.candidates);
  const face = runFace(state, base, outcomes, mode, dispatch);
  return (
    <Panes>
      <FramedPane
        kicker={KICKERS[mode]}
        title={mode === 'logs' ? 'run' : `${total}`}
        width="fill"
        actions={<ModeSwitch mode={mode} options={MODES} zoom={base.zoom} />}
        header={face.header}
      >
        {face.body}
      </FramedPane>
      <NowPane base={base} />
    </Panes>
  );
}
