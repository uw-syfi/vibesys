import {
  type Handoff,
  JobOutcome,
  type Message,
  type Plan,
  PlanStatus,
  type Report,
} from '@vibesys/orchestrator-proto';
import {ms, type OrchestratorState} from '@vibesys/orchestrator-state';
import {Card, Chip, EmptyState, KeyValue, type KeyValueItem, Row, Stack, Text} from '@vibesys/ui';
import type {JSX} from 'react';
import {agentNames, nameOf} from './names.js';
import {formatClock, jobWord, planWord} from './words.js';

/** What is recorded about the claim, or null when nothing is yet. */
export function verdictOf(
  handoff: Handoff,
  plans: readonly Plan[],
  state: OrchestratorState,
  from: string,
): {readonly label: string; readonly tone: 'neutral' | 'live' | 'ok' | 'warn' | 'err'} | null {
  const plan = plans.find(candidate => candidate.number === handoff.planNumber);
  if (
    plan !== undefined &&
    plan.status !== PlanStatus.UNSPECIFIED &&
    plan.status !== PlanStatus.OPEN
  ) {
    const word = planWord(plan.status);
    return {label: `Recorded: ${word.label}`, tone: word.tone};
  }
  const job = Object.values(state.messages).find(
    message =>
      message.body.case === 'jobFinished' && message.body.value.submittedByAgentId === from,
  );
  if (job?.body.case === 'jobFinished' && job.body.value.outcome !== JobOutcome.UNSPECIFIED) {
    const word = jobWord(job.body.value.outcome);
    return {label: `Its benchmark job: ${word.label}`, tone: word.tone};
  }
  return null;
}

function HandoffCard(props: {
  readonly message: Message;
  readonly handoff: Handoff;
  readonly names: ReadonlyMap<string, string>;
  readonly plans: readonly Plan[];
  readonly state: OrchestratorState;
}): JSX.Element {
  const {handoff, message} = props;
  // Only an attempt at an idea has a result to judge: a planner or a reviewer hands off words only.
  const attempt = handoff.planNumber > 0 || handoff.hasCandidate;
  const verdict = attempt
    ? verdictOf(handoff, props.plans, props.state, message.fromAgentId)
    : null;
  const rows: KeyValueItem[] = [
    {key: 'Checks', value: handoff.checks},
    {key: 'Measured', value: handoff.measurement},
    {key: 'Learned', value: handoff.learned},
    {key: 'Suggested next', value: handoff.next},
    {
      key: 'Change',
      value: handoff.commit === '' ? '' : `${handoff.commit} on ${handoff.branch}`,
      mono: true,
    },
  ].filter(row => row.value !== '');
  return (
    <Card
      section={`Handoff from ${nameOf(props.names, message.fromAgentId)}, ${formatClock(ms(message.at) ?? 0)}`}
    >
      <Stack gap="space-3">
        <Row gap="space-3" wrap>
          <Text variant="body-strong">{`It says: ${handoff.claim}`}</Text>
          {!attempt ? null : verdict === null ? (
            <Chip label="Not judged yet" tone="neutral" />
          ) : (
            <Chip label={verdict.label} tone={verdict.tone} dot />
          )}
        </Row>
        {attempt && !handoff.hasCandidate ? (
          <Text tone="muted">No change was kept from this attempt.</Text>
        ) : null}
        <KeyValue items={rows} />
      </Stack>
    </Card>
  );
}

function ReportCard(props: {readonly message: Message; readonly report: Report}): JSX.Element {
  const {report} = props;
  const rows: KeyValueItem[] = [
    {key: 'What changed', value: report.changed},
    {key: 'What was decided', value: report.decisions},
    {key: 'Open for you', value: report.openForUser},
  ].filter(row => row.value !== '');
  return (
    <Card section="Final report">
      <Stack gap="space-3">
        <Text variant="body-strong">{report.headline}</Text>
        <KeyValue items={rows} />
      </Stack>
    </Card>
  );
}

/** Handoffs and reports of a run, newest last, as readable cards. */
export function HandoffCards(props: {
  readonly state: OrchestratorState;
  readonly runId: string;
  readonly plans: readonly Plan[];
}): JSX.Element {
  const names = agentNames(props.state, props.runId);
  const messages = Object.values(props.state.messages)
    .filter(message => message.runId === props.runId)
    .filter(message => message.body.case === 'handoff' || message.body.case === 'report')
    .sort((a, b) => (ms(a.at) ?? 0) - (ms(b.at) ?? 0));
  if (messages.length === 0) {
    return (
      <EmptyState title="Nothing handed off yet" body="Agents write a handoff when they finish." />
    );
  }
  return (
    <Stack gap="space-4">
      {messages.map(message =>
        message.body.case === 'handoff' ? (
          <HandoffCard
            key={message.id}
            message={message}
            handoff={message.body.value}
            names={names}
            plans={props.plans}
            state={props.state}
          />
        ) : message.body.case === 'report' ? (
          <ReportCard key={message.id} message={message} report={message.body.value} />
        ) : null,
      )}
    </Stack>
  );
}
