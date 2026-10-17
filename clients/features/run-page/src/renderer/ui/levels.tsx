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
import type {ResolvedZoom, RunMode, RunPageAction, RunPageState} from '../../state/index.js';
import {IdeasList, ModeSwitch} from './run-level.js';
import {AgentPane, AgentsView} from './views/agents/agents-view.js';
import {IdeaView, StepList} from './views/idea/idea-view.js';
import {LogsView} from './views/logs/logs-view.js';
import type {ViewBase} from './views/props.js';
import {ItemView, StepView} from './views/step/step-view.js';

type Dispatch = (action: RunPageAction) => void;

export interface LevelProps {
  readonly state: RunPageState;
  readonly base: ViewBase;
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
  return (
    <Panes>
      <Pane kicker="ideas" title={`${base.model.candidates.candidates.length}`} width="list">
        <IdeasList
          base={base}
          filter={state.ideas}
          selected={idea.id}
          compact
          dispatch={dispatch}
        />
      </Pane>
      <Pane
        kicker="idea"
        title={idea.title}
        width="fill"
        enterKey={`idea ${idea.id}`}
        meta={<IdeaFacts idea={idea} base={base} />}
        actions={<ModeSwitch mode={mode} options={logsOptions('Steps')} zoom={base.zoom} />}
      >
        {mode === 'logs' ? (
          <LogsView {...base} state={state} scope={{kind: 'idea', idea}} dispatch={dispatch} />
        ) : (
          <IdeaView {...base} idea={idea} />
        )}
      </Pane>
    </Panes>
  );
}

function AgentLevel(props: LevelProps & {readonly agent: AgentNode}): JSX.Element {
  const {base, agent, core} = props;
  return (
    <Panes>
      <Pane kicker="agents" title={`${base.model.agents.agents.length}`} width="list">
        <AgentsView {...base} selected={agent.id} compact />
      </Pane>
      <Pane
        kicker={`agent · ${agent.role}`}
        title={agent.activity.within?.title ?? agent.role}
        width="fill"
        enterKey={`agent ${agent.id}`}
        meta={
          <Chip label={agent.status} tone={agent.status === 'running' ? 'live' : 'neutral'} dot />
        }
      >
        <AgentPane {...base} core={core} agent={agent} />
      </Pane>
    </Panes>
  );
}

function StepPane(
  props: LevelProps & {readonly zoomed: ResolvedZoom; readonly step: CandidateStep},
): JSX.Element {
  const {state, base, core, mode, zoomed, step, dispatch} = props;
  const chip = stepChip(step);
  return (
    <Pane
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
    >
      {mode === 'logs' ? (
        <LogsView {...base} state={state} scope={{kind: 'step', step}} dispatch={dispatch} />
      ) : (
        <StepView
          {...base}
          core={core}
          idea={zoomed.idea}
          agent={zoomed.agent}
          step={step}
          item={null}
          compact={false}
        />
      )}
    </Pane>
  );
}

function StepLevel(
  props: LevelProps & {readonly zoomed: ResolvedZoom; readonly step: CandidateStep},
): JSX.Element {
  const {base, zoomed, step} = props;
  return (
    <Panes>
      {zoomed.idea === null ? (
        <Pane kicker="agents" title={`${base.model.agents.agents.length}`} width="list">
          <AgentsView {...base} selected={zoomed.agent?.id ?? null} compact />
        </Pane>
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
  return (
    <Panes>
      <Pane kicker={step.role ?? step.kind.replaceAll('_', ' ')} title={step.title} width="list">
        <StepView
          {...base}
          core={core}
          idea={zoomed.idea}
          agent={zoomed.agent}
          step={step}
          item={item}
          compact
        />
      </Pane>
      <Pane kicker="item" title={item} width="inspector" enterKey={`item ${item}`}>
        <ItemView {...base} core={core} step={step} item={item} />
      </Pane>
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
