/**
 * The run level: the hero (the best ratio to the input, large, with its value, the input's and the
 * metric) sharing a row with the strip chart, then the panes: the left one switches between Ideas,
 * Timeline, Agents and Logs (`state.zoom` mode), the right one is Now, what runs at this moment,
 * each row a shortcut to its live step.
 */
import {
  Breadcrumb,
  Button,
  Chart,
  type Crumb,
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
  Text,
} from '@vibesys/ui';
import type {JSX} from 'react';
import {
  ageWords,
  bestPathView,
  heroView,
  type IdeaRow,
  type IdeasFilter,
  ideasView,
  type NowItem,
  nowView,
  type RunMode,
  type RunPageAction,
  type RunPageState,
  stripChartView,
  type ZoomEvent,
} from '../../state/index.js';
import {FramedPane} from './framed-pane.js';
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

function Hero(props: ViewBase): JSX.Element {
  const hero = heroView(props.model);
  const chart = stripChartView(props.model, props.clock.start, props.clock.now);
  return (
    <Row gap="space-6" align="center">
      <Stack gap="space-1">
        <Text variant="section">{hero.metric}</Text>
        <Row gap="space-3" align="baseline">
          <Text variant="hero" tone={hero.ratio === null ? 'muted' : 'ok'}>
            {hero.ratio ?? '–'}
          </Text>
          <Stack gap="space-0">
            <Text variant="code">{hero.value ?? 'nothing measured yet'}</Text>
            {hero.input === null ? null : (
              <Text variant="code" tone="muted">
                {hero.input}
              </Text>
            )}
          </Stack>
        </Row>
      </Stack>
      {chart === null ? null : (
        <Stack gap="space-0" grow>
          <Chart
            label={chart.label}
            size="strip"
            points={chart.points}
            steps={chart.steps}
            {...(chart.reference === null ? {} : {reference: chart.reference})}
            {...(chart.playhead === null ? {} : {playhead: chart.playhead})}
            xTicks={chart.xTicks}
            yTicks={chart.yTicks}
            pick={id => props.zoom({type: 'open-idea', idea: id})}
          />
        </Stack>
      )}
    </Row>
  );
}

function BestPath(props: {
  readonly state: RunPageState;
  readonly base: ViewBase;
  readonly dispatch: Dispatch;
}): JSX.Element | null {
  const path = bestPathView(props.base.model, props.state.pathOpen);
  if (path === null) return null;
  const items: Crumb[] = path.crumbs.map((crumb, index) =>
    crumb.kind === 'fold'
      ? {
          id: 'fold',
          label: `… ${crumb.hidden}`,
          press: () => props.dispatch({type: 'best-path-open', open: true}),
        }
      : {
          id: crumb.id,
          label:
            index === path.crumbs.length - 1 && path.ratio !== null
              ? `${crumb.label} ${path.ratio}`
              : crumb.label,
          press: () => props.base.zoom({type: 'open-idea', idea: crumb.id}),
        },
  );
  return (
    <Row gap="space-2" align="center">
      <Text variant="small" tone="muted">
        best path
      </Text>
      <Breadcrumb label="Best path" items={items} />
    </Row>
  );
}

function IdeaPickRow(props: {
  readonly row: IdeaRow;
  readonly compact: boolean;
  readonly now: number | null;
  readonly zoom: Zoom;
}): JSX.Element {
  const {row, compact, zoom} = props;
  return (
    <PickRow
      id={row.id}
      title={row.title}
      mono
      {...(compact || row.summary === null ? {} : {summary: row.summary})}
      state={row.state}
      badges={row.badges}
      {...(compact || row.bar === null ? {} : {bar: row.bar})}
      figure={row.runningSince === null ? row.figure : ageWords(row.runningSince, props.now)}
      figureTone={row.figureTone}
      {...(compact ? {} : {trail: {label: row.trail.label}})}
      stripe={row.stripe}
      fresh={row.fresh}
      select={() => zoom({type: 'open-idea', idea: row.id})}
    />
  );
}

/** The ideas as a flat list: full at the run level, compact beside an opened idea. */
export function IdeasList(props: {
  readonly base: ViewBase;
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
            compact={props.compact}
            now={base.clock.now}
            zoom={base.zoom}
          />
        ))}
      </PickList>
      {view.hiddenRejected === 0 ? null : (
        <Button
          size="sm"
          variant="quiet"
          text={`+ ${view.hiddenRejected} rejected`}
          press={() => dispatch({type: 'ideas-filter', filter: {...filter, rejected: true}})}
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
  const toggle = (key: 'kept' | 'running' | 'rejected') => (on: boolean) =>
    props.dispatch({type: 'ideas-filter', filter: {...filter, [key]: on}});
  return (
    <DotLegend
      items={[
        {tone: 'ok', label: `${counts.kept} kept`, on: filter.kept, toggle: toggle('kept')},
        {
          tone: 'live',
          label: `${counts.running} running`,
          on: filter.running,
          toggle: toggle('running'),
        },
        {
          tone: 'err',
          label: `${counts.rejected} rejected`,
          on: filter.rejected,
          toggle: toggle('rejected'),
        },
      ]}
    />
  );
}

function IdeasFace(props: {
  readonly state: RunPageState;
  readonly base: ViewBase;
  readonly dispatch: Dispatch;
}): JSX.Element {
  const {state, base, dispatch} = props;
  if (base.model.candidates.candidates.length === 0) {
    return <EmptyState title="No ideas yet" body="Each idea the run tries shows here." />;
  }
  return (
    <>
      <BestPath state={state} base={base} dispatch={dispatch} />
      <SearchBox
        label="Search ideas"
        placeholder="Search ideas"
        value={state.ideas.query}
        change={query => dispatch({type: 'ideas-filter', filter: {...state.ideas, query}})}
      />
      <IdeasList
        base={base}
        filter={state.ideas}
        selected={null}
        compact={false}
        dispatch={dispatch}
      />
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
  mode: RunMode,
  dispatch: Dispatch,
): {readonly header: PaneHeader; readonly body: JSX.Element} {
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
      return {header: {}, body: <IdeasFace state={state} base={base} dispatch={dispatch} />};
  }
}

const KICKERS: Readonly<Record<RunMode, string>> = {
  ideas: 'ideas',
  timeline: 'timeline',
  agents: 'agents',
  logs: 'logs',
};

/** The run level: the hero and the chart, then the chosen face and Now. */
export function RunLevel(props: {
  readonly state: RunPageState;
  readonly base: ViewBase;
  readonly mode: RunMode;
  readonly dispatch: Dispatch;
}): JSX.Element {
  const {state, base, mode, dispatch} = props;
  const total =
    mode === 'agents' ? base.model.agents.agents.length : base.model.candidates.candidates.length;
  const face = runFace(state, base, mode, dispatch);
  return (
    <>
      <Hero {...base} />
      <Panes>
        <FramedPane
          kicker={KICKERS[mode]}
          title={mode === 'logs' ? 'run' : `${total}`}
          width="fill"
          {...(mode === 'ideas'
            ? {meta: <IdeasLegend state={state} base={base} dispatch={dispatch} />}
            : {})}
          actions={<ModeSwitch mode={mode} options={MODES} zoom={base.zoom} />}
          header={face.header}
        >
          {face.body}
        </FramedPane>
        <NowPane base={base} />
      </Panes>
    </>
  );
}
