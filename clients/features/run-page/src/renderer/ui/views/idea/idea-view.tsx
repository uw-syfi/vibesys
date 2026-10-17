/**
 * An opened idea's body (the frame draws its pane, with the outcome, ratio and "built on"; the
 * header adds the facts: value, against its parent, how long): its attempts (one line each, the latest selected
 * until another is chosen), the strip of the selected attempt's steps over its time, and those
 * steps (agent turns and gates, each marked by its role's color and named by its role and headline), each opening the step; and the
 * compact step list beside an opened step.
 */
import type {Candidate, CandidateStep} from '@vibesys/run-state/candidates';
import {Chip, EmptyState, PickList, PickRow, Row, Stack, Text} from '@vibesys/ui';
import {type JSX, useState} from 'react';
import type {ImplementerOutcome} from '../../../../state/idea-outcomes.js';
import {roleSeries} from '../../../../state/role-series.js';
import {duration, formatValue, words} from '../../../../state/span-reading.js';
import {
  type AttemptLine,
  attemptLines,
  endOf,
  versusParent,
} from '../../../../state/timeline-rows.js';
import type {ViewBase, ViewHeader, Zoom} from '../props.js';
import {StepStrip} from '../timeline/step-strip.js';

export interface IdeaViewProps extends ViewBase {
  readonly idea: Candidate;
  /** What the idea's implementer reported (its outcome and summary); null when it reported none. */
  readonly outcome: ImplementerOutcome | null;
}

function unitOf(model: ViewBase['model']): string {
  const unit = model.overview?.objective.unit;
  return typeof unit === 'string' ? unit : '';
}

function IdeaFacts(props: IdeaViewProps): JSX.Element | null {
  const {idea, model, clock} = props;
  const unit = unitOf(model);
  const parent =
    idea.parent === null
      ? null
      : (model.candidates.candidates.find(c => c.id === idea.parent) ?? null);
  const value = idea.measurement?.metricValue ?? null;
  const vs = versusParent(idea, parent, model.direction);
  const known = idea.endedAt !== null || clock.now !== null;
  const facts = [
    value === null ? null : `${formatValue(value)}${unit === '' ? '' : ` ${unit}`}`,
    vs === null || parent === null ? null : `${vs} ${parent.title}`,
    known ? duration(endOf(idea.startedAt, idea.endedAt, clock.now) - idea.startedAt) : null,
  ].filter((fact): fact is string => fact !== null);
  if (facts.length === 0) return null;
  return (
    <Text variant="code" tone="muted">
      {facts.join(' · ')}
    </Text>
  );
}

/** The idea's facts after the frame's own (outcome, ratio, built on): value, vs parent, duration. */
export const ideaHeader: ViewHeader<IdeaViewProps> = props => ({
  meta: <IdeaFacts {...props} />,
});

/** The implementer's outcome as a chip and its summary under it. */
function Implemented(props: {readonly outcome: ImplementerOutcome}): JSX.Element {
  const {outcome, summary} = props.outcome;
  return (
    <Stack gap="space-1">
      <Row gap="space-2" align="center">
        <Text variant="small" tone="muted">
          implementer
        </Text>
        {outcome === null ? null : <Chip label={outcome.label} tone={outcome.tone} dot />}
      </Row>
      {summary === null ? null : <Text variant="body">{summary}</Text>}
    </Stack>
  );
}

function Attempts(props: {
  readonly lines: readonly AttemptLine[];
  readonly selected: string;
  readonly choose: (span: string) => void;
}): JSX.Element {
  return (
    <Stack gap="space-1">
      <Text variant="small" tone="muted">
        attempts
      </Text>
      <PickList label="Attempts" selected={props.selected}>
        {props.lines.map(line => (
          <PickRow
            key={line.span}
            id={line.span}
            lead={line.title}
            title={line.result}
            mono
            {...(line.value === null ? {} : {summary: line.value})}
            figure={line.took}
            state={{tone: line.tone, label: line.tone === 'err' ? 'failed' : line.tone}}
            select={() => props.choose(line.span)}
          />
        ))}
      </PickList>
    </Stack>
  );
}

export function IdeaView(props: IdeaViewProps): JSX.Element {
  const {idea, clock, zoom} = props;
  const [chosen, setChosen] = useState<string | null>(null);
  const lines = attemptLines(idea, clock.now, unitOf(props.model));
  const latest = idea.attempts.at(-1);
  const attempt = idea.attempts.find(each => each.span === chosen) ?? latest;
  if (attempt === undefined) return <EmptyState title="No steps yet" />;
  const number = idea.attempts.indexOf(attempt) + 1;
  return (
    <Stack gap="space-3">
      {props.outcome === null ? null : <Implemented outcome={props.outcome} />}
      {lines.length > 1 ? (
        <Attempts lines={lines} selected={attempt.span} choose={setChosen} />
      ) : null}
      <StepStrip idea={idea} attempt={attempt} now={clock.now} zoom={zoom} />
      <StepRows
        steps={attempt.steps}
        label={lines.length > 1 ? `Attempt ${number} steps` : `${idea.title} steps`}
        selected={null}
        zoom={zoom}
        size="default"
      />
    </Stack>
  );
}

export interface StepListProps {
  readonly idea: Candidate;
  /** The opened step's span, highlighted; null when none is open. */
  readonly selected: string | null;
  readonly zoom: Zoom;
}

function stateOf(step: CandidateStep): {tone: 'live' | 'ok' | 'err' | 'neutral'; label: string} {
  switch (step.status) {
    case 'running':
    case 'waiting':
    case 'pending':
      return {tone: 'live', label: step.status};
    case 'done':
      return {tone: 'ok', label: 'done'};
    case 'failed':
      return {tone: 'err', label: 'failed'};
    default:
      return {tone: 'neutral', label: step.status};
  }
}

/** The step's status as a badge, only when it is not the usual finished-ok: failed, running, waiting. */
function noteworthy(step: CandidateStep): ReturnType<typeof stateOf>[] {
  return step.status === 'done' ? [] : [stateOf(step)];
}

/** How long a finished step took; empty while it runs (a list has no clock). */
function tookOf(step: CandidateStep): string | undefined {
  return step.endedAt === null ? undefined : duration(step.endedAt - step.startedAt);
}

function StepRows(props: {
  readonly steps: readonly CandidateStep[];
  readonly label: string;
  readonly selected: string | null;
  readonly zoom: Zoom;
  readonly size: 'default' | 'fill';
}): JSX.Element {
  if (props.steps.length === 0) return <EmptyState title="No steps yet" />;
  return (
    <PickList
      label={props.label}
      size={props.size}
      {...(props.selected === null ? {} : {selected: props.selected})}
    >
      {props.steps.map(step => {
        const took = tookOf(step);
        const name = step.role ?? words(step.kind);
        const badges = noteworthy(step);
        // The role once: a step titled by its role (or untitled) shows it as the title, else led by it.
        const titled = step.title !== '' && step.title !== name;
        return (
          <PickRow
            key={step.span}
            id={step.span}
            {...(titled ? {lead: name} : {})}
            title={titled ? step.title : name}
            {...(step.role === null
              ? {hatched: step.kind === 'gate'}
              : {swatch: roleSeries(step.role)})}
            badges={badges}
            {...(took === undefined ? {} : {figure: took})}
            select={() => props.zoom({type: 'open-step', step: step.span})}
          />
        );
      })}
    </PickList>
  );
}

/** The steps of the idea's latest attempt, each opening its step. */
export function StepList(props: StepListProps): JSX.Element {
  return (
    <StepRows
      steps={props.idea.attempts.at(-1)?.steps ?? []}
      label={`${props.idea.title} steps`}
      selected={props.selected}
      zoom={props.zoom}
      size="fill"
    />
  );
}
