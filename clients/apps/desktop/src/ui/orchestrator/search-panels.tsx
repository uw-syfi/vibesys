import {JobState, oneofIs, type SearchState, UnreadableReason} from '@vibesys/orchestrator-proto';
import {Card, Chip, KeyValue, Row, Stack, Text} from '@vibesys/ui';
import type {JSX} from 'react';
import {
  candidates,
  jobQueue,
  jobsWithNoRun,
  ms,
  planStatus,
  resultSoFar,
} from '../../state/index.js';
import {formatDuration} from './words.js';

/** One wording for every typed unreadable reason, including an unspecified peer response. */
const unreadableWords = {
  [UnreadableReason.UNSPECIFIED]: 'Search state is unavailable.',
  [UnreadableReason.NO_DAEMON]: 'The search service is not running.',
  [UnreadableReason.START_FAILED]: 'The search service could not start.',
  [UnreadableReason.CLI_NOT_INSTALLED]: 'The search CLI is not installed on the host.',
  [UnreadableReason.CLIENT_TOO_OLD]: 'Update VibeSys on the host to read search state.',
  [UnreadableReason.DAEMON_TOO_OLD]: 'Update the search service on the host.',
  [UnreadableReason.STORE_OWNED_ELSEWHERE]: 'Another search service owns this task’s store.',
  [UnreadableReason.STORE_AWAY]: 'The search store is unavailable.',
  [UnreadableReason.STREAM_LOST]: 'The connection to the search service was lost.',
} satisfies Record<UnreadableReason, string>;

/** Search facts stay visible when the daemon cannot read them. Free text is displayed only. */
export function SearchPanels(props: {
  readonly search: SearchState;
  readonly nowMs: number;
}): JSX.Element {
  const {search, nowMs} = props;
  const health = search.health?.state;
  const head = resultSoFar(search);
  const outcome = oneofIs(head, 'current', {current: true, none: true})
    ? head.value.result?.outcome
    : undefined;
  const measure = oneofIs(outcome, 'measure', {check: true, measure: true})
    ? outcome.value
    : undefined;
  return (
    <Stack gap="space-4">
      {oneofIs(health, 'unreadable', {liveSince: true, unreadable: true}) ? (
        <Row gap="space-3" wrap>
          <Chip label="Search state is stale" tone="warn" />
          <Text title={health.value.detail}>{unreadableWords[health.value.reason]}</Text>
          <Text
            variant="small"
            tone="muted"
          >{`Unreadable for ${formatDuration(Math.max(0, nowMs - (ms(health.value.since) ?? nowMs)))}. Previously read facts are kept.`}</Text>
        </Row>
      ) : null}
      <Row gap="space-4" align="start" wrap>
        <Card section="Result so far">
          <Stack gap="space-2">
            <Text>
              {measure === undefined
                ? 'No official measurement of the current result yet.'
                : `${measure.metric?.name ?? 'Score'}: ${measure.value}`}
            </Text>
            {oneofIs(head, 'current', {current: true, none: true}) ? (
              <Text variant="code" title={head.value.head}>
                {head.value.head.slice(0, 12)}
              </Text>
            ) : null}
            <Text variant="small">{`Jobs with no run: ${jobsWithNoRun(search)}`}</Text>
            {search.truncated ? (
              <Text tone="muted">Some search facts were omitted to fit the stream limits.</Text>
            ) : null}
          </Stack>
        </Card>
        <Card section="Candidates">
          <KeyValue
            items={candidates(search).map(row => ({
              key: row.result.commit.slice(0, 12),
              mono: true,
              value: `${oneofIs(row.result.outcome, 'measure', {check: true, measure: true}) ? `${row.result.outcome.value.metric?.name ?? 'Score'} ${row.result.outcome.value.value}` : ''}: ${row.label}${row.stale ? ' (previous head)' : ''}`,
            }))}
          />
        </Card>
        <Card section="Task job queue">
          <Text variant="small" tone="muted">
            Jobs for this task, across runs.
          </Text>
          <KeyValue
            items={jobQueue(search, nowMs).map(({job, elapsedMs, trouble}) => ({
              key: job.id,
              value: `${job.state === JobState.QUEUED ? 'Queued' : 'Running'}, ${formatDuration(elapsedMs)}${trouble === undefined ? '' : `: ${trouble.failure?.message ?? 'Needs attention'}`}`,
            }))}
          />
        </Card>
        <Card section="Plans">
          <KeyValue
            items={planStatus(search).map(({plan, label}) => ({
              key: `Plan ${plan.number}: ${plan.title}`,
              value: label,
            }))}
          />
        </Card>
      </Row>
    </Stack>
  );
}
