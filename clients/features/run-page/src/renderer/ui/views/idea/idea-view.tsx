/**
 * An opened idea's body (the frame draws its pane; the header shows outcome and duration):
 * its attempts (one line each, the latest selected
 * until another is chosen), the strip of the selected attempt's steps over its time, and those
 * steps (agent turns and gates, each marked by its role's color and named by its role and headline), each opening the step; and the
 * compact step list beside an opened step.
 */
import type {TokenLedger} from '@vibesys/run-state';
import type {Candidate, CandidateStep} from '@vibesys/run-state/candidates';
import {
  Chip,
  EmptyState,
  Fact,
  FactLine,
  Markdown,
  PickList,
  PickRow,
  Row,
  type RowBadge,
  Stack,
  Text,
} from '@vibesys/ui';
import {type JSX, useState} from 'react';
import {ideaBases} from '../../../../state/idea-bases.js';
import {effectWords, type IdeaEffect} from '../../../../state/idea-effects.js';
import type {ImplementerOutcome} from '../../../../state/idea-outcomes.js';
import {standingOf, statusLabel} from '../../../../state/idea-words.js';
import {
  ideaOutcomeLine,
  roleLabel,
  spendOf,
  stepLabel,
  stepTitle,
  tokensColumn,
  turnTokens,
  type Who,
  type WhoBySpan,
  whoWords,
} from '../../../../state/index.js';
import {
  pausedSeconds,
  pauseWords,
  type QuotaPauseSpan,
  workedWords,
} from '../../../../state/quota-pauses.js';
import {roleSeries} from '../../../../state/role-series.js';
import {plain, statusWords} from '../../../../state/span-words.js';
import {type AttemptLine, attemptLines, endOf} from '../../../../state/timeline-rows.js';
import type {ViewBase, ViewHeader, Zoom} from '../props.js';
import {StepStrip} from '../timeline/step-strip.js';
import {whoBadge} from '../who-badge.js';

export interface IdeaViewProps extends ViewBase {
  readonly idea: Candidate;
  /** What the idea's implementer reported (its outcome and summary); null when it reported none. */
  readonly outcome: ImplementerOutcome | null;
  /** The framework's verdict on the idea's effect; null when it has none. */
  readonly effect: IdeaEffect | null;
}

function unitOf(model: ViewBase['model']): string {
  const unit = model.overview?.objective.unit;
  return typeof unit === 'string' ? unit : '';
}

const OUTCOME_TONE: Readonly<Record<Candidate['outcome'], 'live' | 'ok' | 'err'>> = {
  running: 'live',
  passed: 'ok',
  failed: 'err',
};

/** The idea's status, or `Best`/`Adopted` when it is the run's leader. */
function standingLabel(props: IdeaViewProps): string {
  return standingOf(props.model.candidates.best, props.idea.id) ?? statusLabel(props.idea);
}

/**
 * The idea's outcome and duration. Its value against the input is in the performance row above.
 */
function IdeaFacts(props: IdeaViewProps): JSX.Element {
  const {idea, clock} = props;
  const {pauses} = props.model;
  const known = idea.endedAt !== null || clock.now !== null;
  const end = endOf(idea.startedAt, idea.endedAt, clock.now);
  const paused = pausedSeconds(pauses, idea.startedAt, end);
  return (
    <FactLine>
      <Fact>
        <Chip label={standingLabel(props)} tone={OUTCOME_TONE[idea.outcome]} dot />
      </Fact>
      {known ? (
        <Fact>
          <Text variant="code" tone="muted">
            {workedWords(pauses, idea.startedAt, end)}
          </Text>
        </Fact>
      ) : null}
      {known && paused > 0 ? (
        <Fact>
          <Text variant="code" tone="muted">
            {pauseWords(paused)}
          </Text>
        </Fact>
      ) : null}
    </FactLine>
  );
}

/** The idea's facts as the pane's meta line. */
export const ideaHeader: ViewHeader<IdeaViewProps> = props => ({
  meta: <IdeaFacts {...props} />,
});

/** Who ran the idea's implementer: its latest implementer turn that the records name. */
function implementerWho(idea: Candidate, who: WhoBySpan): Who | undefined {
  const turns = idea.attempts
    .flatMap(attempt => attempt.steps)
    .filter(step => step.kind === 'agent_turn' && step.role === 'implementer');
  for (const step of turns.reverse()) {
    const ran = who.get(step.span);
    if (ran !== undefined) return ran;
  }
  return undefined;
}

/** The implementer's outcome as a chip (else the idea's own) and its summary under it. */
function Implemented(props: {
  readonly outcome: ImplementerOutcome;
  /** What ran the implementer's turn, in words; null when the records do not say. */
  readonly ran: string | null;
  /** The idea's own outcome, said when the implementer reported none. */
  readonly idea: Candidate;
}): JSX.Element {
  const {outcome, summary} = props.outcome;
  return (
    <Stack gap="space-1">
      <Row gap="space-2" align="center">
        <Text variant="small" tone="muted">
          {roleLabel('implementer')}
        </Text>
        {props.ran === null ? null : (
          <Text variant="code" tone="muted">
            {props.ran}
          </Text>
        )}
        {outcome === null ? (
          <Chip label={statusLabel(props.idea)} tone={OUTCOME_TONE[props.idea.outcome]} dot />
        ) : (
          <Chip label={outcome.label} tone={outcome.tone} dot />
        )}
      </Row>
      {summary === null ? null : <Markdown text={summary} />}
    </Stack>
  );
}

/**
 * What the idea built on and the framework's verdict on its effect, in a line every idea has so
 * the content below never moves between ideas (a header that wrapped would shift it).
 */
function BaseAndEffect(props: IdeaViewProps): JSX.Element {
  const base = ideaBases(props.model.candidates, props.model.direction).get(props.idea.id);
  const from = base?.kind === 'own work' ? 'from its own work' : `from ${base?.label ?? 'input'}`;
  return (
    <Text variant="small" tone="muted">
      {[from, props.effect === null ? null : effectWords(props.effect)]
        .filter(part => part !== null)
        .join(' · ')}
    </Text>
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
  const lines = attemptLines(idea, clock.now, unitOf(props.model), props.model.pauses);
  const latest = idea.attempts.at(-1);
  const attempt = idea.attempts.find(each => each.span === chosen) ?? latest;
  if (attempt === undefined) return <EmptyState title="No steps yet" />;
  const number = idea.attempts.indexOf(attempt) + 1;
  return (
    <Stack gap="space-3">
      <Stack gap="space-1">
        <Text variant="small">{ideaOutcomeLine(props.model, idea)}</Text>
        <BaseAndEffect {...props} />
      </Stack>
      {props.outcome === null ? null : (
        <Implemented
          outcome={props.outcome}
          idea={idea}
          ran={whoWords(implementerWho(idea, props.who))}
        />
      )}
      {lines.length > 1 ? (
        <Attempts lines={lines} selected={attempt.span} choose={setChosen} />
      ) : null}
      <StepStrip
        idea={idea}
        attempt={attempt}
        now={clock.now}
        pauses={props.model.pauses}
        zoom={zoom}
      />
      <StepRows
        steps={attempt.steps}
        label={lines.length > 1 ? `Attempt ${number} steps` : `${idea.title} steps`}
        selected={null}
        tokens={props.tokens}
        who={props.who}
        pauses={props.model.pauses}
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
  /** What the run's turns spent, for the figure beside each turn's duration. */
  readonly tokens: TokenLedger;
  /** What ran each turn, for the chip beside the role. */
  readonly who: WhoBySpan;
  /** Where turns were stopped on a capacity limit, left out of each step's duration. */
  readonly pauses: readonly QuotaPauseSpan[];
  readonly zoom: Zoom;
}

function stateOf(step: CandidateStep): {tone: 'live' | 'ok' | 'err' | 'neutral'; label: string} {
  switch (step.status) {
    case 'running':
    case 'waiting':
    case 'pending':
      return {tone: 'live', label: statusWords(step.status).label};
    case 'done':
      return {tone: 'ok', label: 'Done'};
    case 'failed':
      return {tone: 'err', label: 'Failed'};
    default:
      return {tone: 'neutral', label: statusWords(step.status).label};
  }
}

/** The step's status as a badge, only when it is not the usual finished-ok: failed, running, waiting. */
function noteworthy(step: CandidateStep): ReturnType<typeof stateOf>[] {
  return step.status === 'done' ? [] : [stateOf(step)];
}

/** What ran an agent turn, the pause it sat out when it did, then its status when that is noteworthy. */
function stepBadges(
  step: CandidateStep,
  who: WhoBySpan,
  pauses: readonly QuotaPauseSpan[],
): RowBadge[] {
  const ran = step.kind === 'agent_turn' ? whoBadge(who.get(step.span)) : null;
  const paused = step.endedAt === null ? 0 : pausedSeconds(pauses, step.startedAt, step.endedAt);
  return [
    ...(ran === null ? [] : [ran]),
    ...(paused > 0 ? [{label: pauseWords(paused), tone: 'warn' as const}] : []),
    ...noteworthy(step),
  ];
}

/** How long a finished step took, its quota pauses left out; empty while it runs (a list has no clock). */
function tookOf(step: CandidateStep, pauses: readonly QuotaPauseSpan[]): string | undefined {
  return step.endedAt === null ? undefined : workedWords(pauses, step.startedAt, step.endedAt);
}

function StepRows(props: {
  readonly steps: readonly CandidateStep[];
  readonly label: string;
  readonly selected: string | null;
  readonly tokens: TokenLedger;
  readonly who: WhoBySpan;
  readonly pauses: readonly QuotaPauseSpan[];
  readonly zoom: Zoom;
  readonly size: 'default' | 'fill';
}): JSX.Element {
  if (props.steps.length === 0) return <EmptyState title="No steps yet" />;
  const cells = tokensColumn(
    props.steps.map(step =>
      step.kind === 'agent_turn'
        ? spendOf(turnTokens(props.tokens, step.span), step.status)
        : undefined,
    ),
  );
  return (
    <PickList
      label={props.label}
      size={props.size}
      {...(props.selected === null ? {} : {selected: props.selected})}
    >
      {props.steps.map((step, index) => {
        const took = tookOf(step, props.pauses);
        const name = step.role === null ? plain(step.kind) : stepLabel(step);
        // Every row reads alike: its mark, its name (the role, or the kind for a gate), then the
        // step's headline after it when it has one beyond the name.
        const headline = stepTitle(step);
        const titled = headline !== '' && headline !== name;
        const measure = cells[index] ?? null;
        return (
          <PickRow
            key={step.span}
            id={step.span}
            title={name}
            {...(titled ? {summary: headline} : {})}
            {...(step.role === null
              ? {hatched: step.kind === 'gate'}
              : {swatch: roleSeries(step.role)})}
            badges={stepBadges(step, props.who, props.pauses)}
            {...(took === undefined ? {} : {figure: took})}
            {...(measure === null ? {} : {measure: {...measure, size: 'line' as const}})}
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
      tokens={props.tokens}
      who={props.who}
      pauses={props.pauses}
      zoom={props.zoom}
      size="fill"
    />
  );
}
