/**
 * The levels below the run, at most two side by side: the list chosen from on the left and what
 * was chosen on the right. An idea beside the ideas; an agent beside the agents; a step beside its
 * idea's steps (or the agents); an item (level 3) beside its step. The frame draws each pane (its
 * kicker, title and facts); the views (`views/`) draw what is in them.
 */

import type {CoreState} from '@vibesys/run-state';
import type {AgentNode} from '@vibesys/run-state/agents';
import type {Candidate, CandidateStep} from '@vibesys/run-state/candidates';
import {Button, Chip, Pane, Panes, Row, Text} from '@vibesys/ui';
import type {JSX} from 'react';
import {
  implementerOutcomes,
  type ResolvedZoom,
  type RunMode,
  type RunModel,
  type RunPageAction,
  type RunPageState,
} from '../../state/index.js';
import {FramedPane} from './framed-pane.js';
import {agentFolds, IdeasList, ModeSwitch} from './run-level.js';
import {
  AgentPane,
  type AgentPaneProps,
  AgentsView,
  type AgentsViewProps,
} from './views/agents/agents-view.js';
import {
  agentHeader,
  agentsHeader,
  ideaHeader,
  itemHeader,
  logsHeader,
  stepHeader,
} from './views/headers.js';
import {IdeaView, type IdeaViewProps, StepList} from './views/idea/idea-view.js';
import {LogsView, type LogsViewProps} from './views/logs/logs-view.js';
import type {ViewBase} from './views/props.js';
import {
  ItemView,
  type ItemViewProps,
  StepView,
  type StepViewProps,
} from './views/step/step-view.js';

type Dispatch = (action: RunPageAction) => void;

export interface LevelProps {
  readonly state: RunPageState;
  readonly base: ViewBase;
  /** The run to its live end, for what must not change while the reader scrubs. */
  readonly whole: RunModel;
  readonly core: CoreState | null;
  readonly mode: RunMode;
  readonly dispatch: Dispatch;
}

const OUTCOME: Readonly<
  Record<Candidate['outcome'], {tone: 'live' | 'ok' | 'err'; label: string}>
> = {
  running: {tone: 'live', label: 'Running'},
  kept: {tone: 'ok', label: 'Kept'},
  rejected: {tone: 'err', label: 'Rejected'},
};

function stepChip(step: CandidateStep): {tone: 'live' | 'ok' | 'err' | 'neutral'; label: string} {
  if (step.status === 'running' || step.status === 'waiting' || step.status === 'pending') {
    return {tone: 'live', label: 'Running'};
  }
  if (step.status === 'failed') return {tone: 'err', label: 'Failed'};
  return step.status === 'done'
    ? {tone: 'ok', label: 'Done'}
    : {tone: 'neutral', label: step.status};
}

function logsOptions(main: string): {value: RunMode; label: string}[] {
  return [
    {value: 'ideas', label: main},
    {value: 'logs', label: 'Logs'},
  ];
}

function IdeaFacts(props: {readonly idea: Candidate; readonly base: ViewBase}): JSX.Element {
  const {idea, base} = props;
  const parent =
    idea.parent === null
      ? null
      : (base.model.candidates.candidates.find(item => item.id === idea.parent) ?? null);
  const vs = idea.measurement?.vsBaseline ?? null;
  return (
    <>
      <Chip label={OUTCOME[idea.outcome].label} tone={OUTCOME[idea.outcome].tone} dot />
      {vs === null ? null : (
        <Text variant="metric" tone={idea.outcome === 'kept' ? 'ok' : 'err'}>
          {`${vs.toFixed(2)}×`}
        </Text>
      )}
      <Row gap="space-1" align="center">
        <Text variant="small" tone="muted">
          built on
        </Text>
        {parent === null ? (
          <Text variant="small">input</Text>
        ) : (
          <Button
            size="sm"
            variant="quiet"
            text={parent.title}
            press={() => base.zoom({type: 'open-idea', idea: parent.id})}
          />
        )}
      </Row>
    </>
  );
}

function IdeaLevel(props: LevelProps & {readonly idea: Candidate}): JSX.Element {
  const {state, base, mode, idea, dispatch} = props;
  const outcomes = implementerOutcomes(props.core, base.model.candidates);
  const view: IdeaViewProps = {...base, idea, outcome: outcomes.get(idea.id) ?? null};
  const logs: LogsViewProps = {...base, state, scope: {kind: 'idea', idea}, dispatch};
  return (
    <Panes>
      <Pane kicker="ideas" title={`${base.model.candidates.candidates.length}`} width="list">
        <IdeasList
          base={base}
          outcomes={outcomes}
          filter={state.ideas}
          selected={idea.id}
          compact
          dispatch={dispatch}
        />
      </Pane>
      <FramedPane
        kicker="idea"
        title={idea.title}
        width="fill"
        enterKey={`idea ${idea.id}`}
        meta={<IdeaFacts idea={idea} base={base} />}
        actions={<ModeSwitch mode={mode} options={logsOptions('Steps')} zoom={base.zoom} />}
        header={mode === 'logs' ? logsHeader(logs) : ideaHeader(view)}
      >
        {mode === 'logs' ? <LogsView {...logs} /> : <IdeaView {...view} />}
      </FramedPane>
    </Panes>
  );
}

function AgentLevel(props: LevelProps & {readonly agent: AgentNode}): JSX.Element {
  const {base, agent, core} = props;
  const view: AgentPaneProps = {...base, core, agent};
  return (
    <Panes>
      <AgentsPane {...props} selected={agent.id} />
      <FramedPane
        kicker={`agent · ${agent.role}`}
        title={agent.activity.within?.title ?? agent.role}
        width="fill"
        enterKey={`agent ${agent.id}`}
        meta={
          <Chip label={agent.status} tone={agent.status === 'running' ? 'live' : 'neutral'} dot />
        }
        header={agentHeader(view)}
      >
        <AgentPane {...view} />
      </FramedPane>
    </Panes>
  );
}

/** The agents, compact, as the list beside an opened agent or an agent's turn. */
function AgentsPane(props: LevelProps & {readonly selected: string | null}): JSX.Element {
  const {base} = props;
  const view: AgentsViewProps = {
    ...base,
    ...agentFolds(props.state, props.dispatch),
    selected: props.selected,
    compact: true,
  };
  return (
    <FramedPane
      kicker="agents"
      title={`${base.model.agents.agents.length}`}
      width="list"
      header={agentsHeader(view)}
    >
      <AgentsView {...view} />
    </FramedPane>
  );
}

function StepPane(
  props: LevelProps & {readonly zoomed: ResolvedZoom; readonly step: CandidateStep},
): JSX.Element {
  const {state, base, core, mode, zoomed, step, dispatch} = props;
  const chip = stepChip(step);
  const view: StepViewProps = {
    ...base,
    core,
    idea: zoomed.idea,
    agent: zoomed.agent,
    step,
    item: null,
    compact: false,
  };
  const logs: LogsViewProps = {...base, state, scope: {kind: 'step', step}, dispatch};
  return (
    <FramedPane
      kicker={
        step.role === null
          ? step.kind.replaceAll('_', ' ')
          : `${step.kind.replaceAll('_', ' ')} · ${step.role}`
      }
      title={step.title}
      width="fill"
      enterKey={`step ${step.span}`}
      meta={<Chip label={chip.label} tone={chip.tone} dot />}
      actions={<ModeSwitch mode={mode} options={logsOptions('Step')} zoom={base.zoom} />}
      header={mode === 'logs' ? logsHeader(logs) : stepHeader(view)}
    >
      {mode === 'logs' ? <LogsView {...logs} /> : <StepView {...view} />}
    </FramedPane>
  );
}

function StepLevel(
  props: LevelProps & {readonly zoomed: ResolvedZoom; readonly step: CandidateStep},
): JSX.Element {
  const {base, zoomed, step} = props;
  return (
    <Panes>
      {zoomed.idea === null ? (
        <AgentsPane {...props} selected={zoomed.agent?.id ?? null} />
      ) : (
        <Pane kicker="steps" title={zoomed.idea.title} width="list">
          <StepList idea={zoomed.idea} selected={step.span} zoom={base.zoom} />
        </Pane>
      )}
      <StepPane {...props} />
    </Panes>
  );
}

function ItemLevel(
  props: LevelProps & {
    readonly zoomed: ResolvedZoom;
    readonly step: CandidateStep;
    readonly item: string;
  },
): JSX.Element {
  const {base, core, zoomed, step, item} = props;
  const view: StepViewProps = {
    ...base,
    core,
    idea: zoomed.idea,
    agent: zoomed.agent,
    step,
    item,
    compact: true,
  };
  const opened: ItemViewProps = {...base, core, step, item};
  return (
    <Panes>
      <FramedPane
        kicker={step.role ?? step.kind.replaceAll('_', ' ')}
        title={step.title}
        width="list"
        header={stepHeader(view)}
      >
        <StepView {...view} />
      </FramedPane>
      <FramedPane
        kicker="item"
        title={item}
        width="inspector"
        enterKey={`item ${item}`}
        header={itemHeader(opened)}
      >
        <ItemView {...opened} />
      </FramedPane>
    </Panes>
  );
}

/** The level the resolved zoom names, below the run. */
export function DeepLevel(props: LevelProps & {readonly zoomed: ResolvedZoom}): JSX.Element | null {
  const {zoomed} = props;
  if (zoomed.step !== null && zoomed.item !== null) {
    return <ItemLevel {...props} step={zoomed.step} item={zoomed.item} />;
  }
  if (zoomed.step !== null) return <StepLevel {...props} step={zoomed.step} />;
  if (zoomed.idea !== null) return <IdeaLevel {...props} idea={zoomed.idea} />;
  if (zoomed.agent !== null) return <AgentLevel {...props} agent={zoomed.agent} />;
  return null;
}
