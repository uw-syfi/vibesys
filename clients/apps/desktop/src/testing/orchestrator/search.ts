/** Scripted typed search facts shared by the fixture page and pure view tests. */
import {create} from '@bufbuild/protobuf';
import {timestampFromMs} from '@bufbuild/protobuf/wkt';
import {
  JobState,
  PlanStatus,
  SearchStateSchema,
  UnreadableReason,
  VerdictKind,
} from '@vibesys/orchestrator-proto';

export function searchFixture(runId: string, nowMs: number, stale = false) {
  const commit = 'a'.repeat(40);
  const head = 'b'.repeat(40);
  const result = {
    jobId: 'j1',
    commit,
    outcome: {case: 'measure' as const, value: {metric: {name: 'Throughput'}, value: 42}},
  };
  return create(SearchStateSchema, {
    runId,
    revision: 1n,
    health: {
      state: stale
        ? {
            case: 'unreadable',
            value: {reason: UnreadableReason.STREAM_LOST, since: timestampFromMs(nowMs)},
          }
        : {case: 'liveSince', value: timestampFromMs(nowMs)},
    },
    results: [result],
    head: {state: {case: 'current', value: {head, result: {...result, commit: head}}}},
    judgments: [
      {
        commit,
        against: head,
        outcome: {case: 'comparison', value: {verdict: {kind: VerdictKind.BETTER}}},
      },
    ],
    jobs: [
      {
        id: 'j2',
        state: JobState.RUNNING,
        submittedAt: timestampFromMs(nowMs - 30000),
        startedAt: timestampFromMs(nowMs - 20000),
      },
      {id: 'j3', state: JobState.QUEUED, submittedAt: timestampFromMs(nowMs - 10000)},
    ],
    plans: [{number: 1, title: 'Reduce contention', status: PlanStatus.CHOSEN}],
    unattributedJobs: 2,
    truncated: true,
  });
}
