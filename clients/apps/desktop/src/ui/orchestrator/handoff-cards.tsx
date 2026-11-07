import {
  type Handoff,
  JobOutcome,
  JudgeVerdict,
  type Message,
  type Report,
} from '@vibesys/orchestrator-proto';
import {Card, Chip, EmptyState, KeyValue, type KeyValueItem, Row, Stack, Text} from '@vibesys/ui';
import type {JSX} from 'react';
import {messageIs, ms, type OrchestratorState} from '../../state/index.js';
import {agentNames, nameOf} from './names.js';
import {formatClock, jobWord} from './words.js';

/** What is recorded about the claim, or null when nothing is yet. */
export function verdictOf(
  state: OrchestratorState,
  from: string,
): {readonly label: string; readonly tone: 'neutral' | 'live' | 'ok' | 'warn' | 'err'} | null {
  const job = Object.values(state.messages).find(
    message => messageIs(message, 'jobFinished') && message.body.value.submittedByAgentId === from,
  );
  if (messageIs(job, 'jobFinished') && job.body.value.outcome !== JobOutcome.UNSPECIFIED) {
    const word = jobWord(job.body.value.outcome);
    return {label: `Its benchmark job: ${word.label}`, tone: word.tone};
  }
  return null;
}

const VERDICT_WORDS: Record<JudgeVerdict, string> = {
  [JudgeVerdict.UNSPECIFIED]: '',
  [JudgeVerdict.PASS]: 'The claim holds',
  [JudgeVerdict.FAIL]: 'The claim does not hold',
  [JudgeVerdict.UNMEASURED]: 'It could not be checked',
};

/** What the lead was told beyond the claim: the body of the role that wrote the handoff. */
function bodyRows(handoff: Handoff): {
  readonly attempt: boolean;
  readonly kept: boolean;
  readonly rows: KeyValueItem[];
} {
  const body = handoff.body;
  switch (body.case) {
    case 'worker': {
      const outcome = body.value.outcome;
      switch (outcome.case) {
        case 'candidate':
          return {
            attempt: true,
            kept: true,
            rows: [
              {key: 'Checks', value: outcome.value.checks},
              {key: 'Measured (the agent’s words)', value: outcome.value.measurement},
              {
                key: 'Change',
                value: `${outcome.value.commit} on ${outcome.value.branch}`,
                mono: true,
              },
            ],
          };
        case 'noCandidate':
          return {
            attempt: true,
            kept: false,
            rows: [{key: 'What stopped it', value: outcome.value.blocker}],
          };
        case undefined:
          return {attempt: true, kept: false, rows: []};
        default:
          outcome satisfies never;
          return {attempt: true, kept: false, rows: []};
      }
    }
    case 'planner':
      return {
        attempt: false,
        kept: false,
        rows: [
          {key: 'Plan', value: body.value.plan, mono: true},
          {key: 'Confidence', value: body.value.confidence},
          {key: 'Not established', value: body.value.unestablished},
          {key: 'Measurement that would change its view', value: body.value.decidingMeasurement},
        ],
      };
    case 'judge':
      return {
        attempt: false,
        kept: false,
        rows: [
          {key: 'Verdict', value: VERDICT_WORDS[body.value.verdict]},
          {key: 'Reasons', value: body.value.reasons},
          {key: 'Change judged', value: body.value.commit, mono: true},
        ],
      };
    case 'combiner':
      return {
        attempt: true,
        kept: true,
        rows: [
          {key: 'Applied', value: body.value.applied},
          {key: 'Not applied', value: body.value.unapplied},
          {key: 'Checks', value: body.value.checks},
          {key: 'Measured (the agent’s words)', value: body.value.measurement},
          {key: 'Change', value: `${body.value.commit} on ${body.value.branch}`, mono: true},
        ],
      };
    case 'profiler':
      return {
        attempt: false,
        kept: false,
        rows: [
          {key: 'Question', value: body.value.question},
          {key: 'Answer', value: body.value.answer},
          {key: 'Evidence', value: body.value.evidence},
          {key: 'Not answered', value: body.value.unsupported},
          {key: 'Informs', value: body.value.informs},
        ],
      };
    case undefined:
      return {attempt: false, kept: false, rows: []};
    default:
      body satisfies never;
      return {attempt: false, kept: false, rows: []};
  }
}

function HandoffCard(props: {
  readonly message: Message;
  readonly handoff: Handoff;
  readonly names: ReadonlyMap<string, string>;
  readonly state: OrchestratorState;
}): JSX.Element {
  const {handoff, message} = props;
  // Only an attempt at an idea has a result to judge: a planner, a reviewer or a profiler hands off words only.
  const {attempt, kept, rows: bodyOnly} = bodyRows(handoff);
  const verdict = attempt ? verdictOf(props.state, message.fromAgentId) : null;
  const rows: KeyValueItem[] = [
    ...bodyOnly,
    {key: 'Learned', value: handoff.learned},
    {key: 'Suggested next', value: handoff.next},
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
        {attempt && !kept ? <Text tone="muted">No change was kept from this attempt.</Text> : null}
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
    .filter(message => messageIs(message, 'handoff') || messageIs(message, 'report'))
    .sort((a, b) => (ms(a.at) ?? 0) - (ms(b.at) ?? 0));
  if (messages.length === 0) {
    return (
      <EmptyState title="Nothing handed off yet" body="Agents write a handoff when they finish." />
    );
  }
  return (
    <Stack gap="space-4" align="start">
      {messages.map(message =>
        messageIs(message, 'handoff') ? (
          <HandoffCard
            key={message.id}
            message={message}
            handoff={message.body.value}
            names={names}
            state={props.state}
          />
        ) : messageIs(message, 'report') ? (
          <ReportCard key={message.id} message={message} report={message.body.value} />
        ) : null,
      )}
    </Stack>
  );
}
