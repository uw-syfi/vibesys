import {type SearchState, UnreadableReason} from '@vibesys/orchestrator-proto';
import {Card, KeyValue, Stack, Text} from '@vibesys/ui';
import type {JSX} from 'react';
import {
  candidates,
  jobQueue,
  jobsWithNoRun,
  planStatus,
  resultSoFar,
} from '../../state/orchestrator/search-views.js';

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
  const outcome = head?.case === 'current' ? head.value.result?.outcome : undefined;
  const measure = outcome?.case === 'measure' ? outcome.value : undefined;
  return (
    <Stack gap="space-4">
      {health?.case === 'unreadable' ? (
        <Card section="Search state is stale">
          <Stack gap="space-2">
            <Text tone="warn">{unreadableWords[health.value.reason]}</Text>
            <Text tone="muted">Previously read facts are kept and may be out of date.</Text>
            {health.value.detail === '' ? null : <Text variant="small">{health.value.detail}</Text>}
          </Stack>
        </Card>
      ) : null}
      <Card section="Result so far">
        <Stack gap="space-2">
          <Text>
            {measure === undefined
              ? 'No official measurement of the current result yet.'
              : `${measure.metric?.name ?? 'Score'}: ${measure.value}`}
          </Text>
          {head?.case === 'current' ? (
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
            value: `${row.result.outcome.case === 'measure' ? `${row.result.outcome.value.metric?.name ?? 'Score'} ${row.result.outcome.value.value}` : ''}: ${row.label}${row.stale ? ' (previous head)' : ''}`,
          }))}
        />
      </Card>
      <Card section="Job queue">
        <KeyValue
          items={jobQueue(search, nowMs).map(({job, elapsedMs, trouble}) => ({
            key: job.id,
            value: `${job.startedAt === undefined ? 'Queued' : 'Running'}, ${Math.floor(elapsedMs / 1000)}s${trouble === undefined ? '' : `: ${trouble.failure?.message ?? 'Needs attention'}`}`,
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
    </Stack>
  );
}
