import {
  type Handoff,
  JobOutcome,
  type Message,
  oneofIs,
  type Report,
} from '@vibesys/orchestrator-proto';
import {Card, Chip, EmptyState, KeyValue, type KeyValueItem, Row, Stack, Text} from '@vibesys/ui';
import type {JSX} from 'react';
import {ms, type OrchestratorState} from '../../state/index.js';
import {agentNames, nameOf} from './names.js';
import {formatClock, jobWord} from './words.js';

/** What is recorded about the claim, or null when nothing is yet. */
export function verdictOf(
  state: OrchestratorState,
  from: string,
): {readonly label: string; readonly tone: 'neutral' | 'live' | 'ok' | 'warn' | 'err'} | null {
  const job = Object.values(state.messages).find(
    message =>
      oneofIs(message.body, 'jobFinished', {
        text: true,
        handoff: true,
        report: true,
        jobFinished: true,
        subagentSettled: true,
        brief: true,
        leadBrief: true,
        handoffReminder: true,
        leadIdleReminder: true,
      }) && message.body.value.submittedByAgentId === from,
  );
  if (
    oneofIs(job?.body, 'jobFinished', {
      text: true,
      handoff: true,
      report: true,
      jobFinished: true,
      subagentSettled: true,
      brief: true,
      leadBrief: true,
      handoffReminder: true,
      leadIdleReminder: true,
    }) &&
    job.body.value.outcome !== JobOutcome.UNSPECIFIED
  ) {
    const word = jobWord(job.body.value.outcome);
    return {label: `Its benchmark job: ${word.label}`, tone: word.tone};
  }
  return null;
}

function HandoffCard(props: {
  readonly message: Message;
  readonly handoff: Handoff;
  readonly names: ReadonlyMap<string, string>;
  readonly state: OrchestratorState;
}): JSX.Element {
  const {handoff, message} = props;
  // Only an attempt at an idea has a result to judge: a planner or a reviewer hands off words only.
  const attempt = handoff.planNumber > 0 || handoff.hasCandidate;
  const verdict = attempt ? verdictOf(props.state, message.fromAgentId) : null;
  const rows: KeyValueItem[] = [
    {key: 'Checks', value: handoff.checks},
    {key: 'Measured (the agent’s words)', value: handoff.measurement},
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
}): JSX.Element {
  const names = agentNames(props.state, props.runId);
  const messages = Object.values(props.state.messages)
    .filter(message => message.runId === props.runId)
    .filter(
      message =>
        oneofIs(message.body, 'handoff', {
          text: true,
          handoff: true,
          report: true,
          jobFinished: true,
          subagentSettled: true,
          brief: true,
          leadBrief: true,
          handoffReminder: true,
          leadIdleReminder: true,
        }) ||
        oneofIs(message.body, 'report', {
          text: true,
          report: true,
          jobFinished: true,
          subagentSettled: true,
          brief: true,
          leadBrief: true,
          handoffReminder: true,
          leadIdleReminder: true,
        }),
    )
    .sort((a, b) => (ms(a.at) ?? 0) - (ms(b.at) ?? 0));
  if (messages.length === 0) {
    return (
      <EmptyState title="Nothing handed off yet" body="Agents write a handoff when they finish." />
    );
  }
  return (
    <Stack gap="space-4" align="start">
      {messages.map(message =>
        oneofIs(message.body, 'handoff', {
          text: true,
          handoff: true,
          report: true,
          jobFinished: true,
          subagentSettled: true,
          brief: true,
          leadBrief: true,
          handoffReminder: true,
          leadIdleReminder: true,
        }) ? (
          <HandoffCard
            key={message.id}
            message={message}
            handoff={message.body.value}
            names={names}
            state={props.state}
          />
        ) : oneofIs(message.body, 'report', {
            text: true,
            report: true,
            jobFinished: true,
            subagentSettled: true,
            brief: true,
            leadBrief: true,
            handoffReminder: true,
            leadIdleReminder: true,
          }) ? (
          <ReportCard key={message.id} message={message} report={message.body.value} />
        ) : null,
      )}
    </Stack>
  );
}
