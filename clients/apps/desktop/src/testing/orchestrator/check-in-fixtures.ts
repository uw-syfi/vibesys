/**
 * Check-ins already on a fake host, for the fixture page: a viewer who has caught up on a run
 * before, in each state a screen can show. The windows follow one another, the
 * figures are the fixture's (nothing here works out a window), and the lead's words are what a
 * lead would write. Made by pressing Catch me up on the fake host, so every rule it applies to a
 * press (windows, the ask, the pending check-in answered again) holds for what is seeded.
 */
import {create} from '@bufbuild/protobuf';
import {AgentRole, CommandSchema, ObjectiveDirection} from '@vibesys/orchestrator-proto';
import {ms} from '../../state/index.js';
import {bestValue, roleSpend, windowFactsOf} from './check-ins.js';
import type {FakeDaemon} from './fake-daemon.js';
import type {Scenario} from './scenario.js';

/** How far along the viewer's catch-ups are. */
export type CatchUpShape =
  /** No check-in. */
  | 'none'
  /** Four ready check-ins, one of them failed, the newest written about. */
  | 'history'
  /** The newest has its facts and the lead is still writing. */
  | 'writing'
  /** The newest has its facts and the lead's summary. */
  | 'written'
  /** The newest has neither: the host is still working out its facts. */
  | 'pending'
  /** The newest failed. */
  | 'failed';

export const CATCH_UP_SHAPES: readonly CatchUpShape[] = [
  'none',
  'history',
  'writing',
  'written',
  'pending',
  'failed',
];

const SECOND = 1000;

const press = (daemon: FakeDaemon, scenario: Scenario, id: string, atMs: number): string => {
  const receipt = daemon.dispatch(
    create(CommandSchema, {id, body: {case: 'requestCatchUp', value: {runId: scenario.runId}}}),
    atMs,
  );
  return receipt.checkInId;
};

const REPORT = [
  'Best moved from **1.84x** to **2.31x** at p99 118 ms. The hipGraph decode loop and fused expert dispatch were kept.',
  'Three ideas were rejected, including the FP8 KV cache, which failed accuracy. The prefetch pipeline is still open.',
].join('\n\n');

/**
 * Press Catch me up as the fake host's viewer, `shape` says how often and how it came out. The
 * newest window ends where the scenario ends, so it is the one a viewer who comes back now sees.
 */
export function seedCheckIns(daemon: FakeDaemon, scenario: Scenario, shape: CatchUpShape): void {
  if (shape === 'none') return;
  const end = scenario.endMs;
  const startedAt = ms(daemon.snapshot().runs.find(run => run.id === scenario.runId)?.createdAt);
  // The windows follow one another through the run, and the newest ends where the scenario does.
  const along = (share: number): number =>
    Math.round((startedAt ?? end) + (end - (startedAt ?? end)) * share);
  const [firstAt, secondAt, thirdAt] = [along(0.3), along(0.55), along(0.8)];
  const settle = (id: string, atMs: number, rounds: number, unpriced = 1) =>
    daemon.settleCheckIn(
      id,
      windowFactsOf({
        roles: [
          roleSpend(AgentRole.LEAD, 3 + rounds, 0, 0.4 * rounds),
          roleSpend(AgentRole.WORKER, 4 * rounds, unpriced, 3.1 * rounds),
          roleSpend(AgentRole.JUDGE, rounds, 0, 0.6),
        ],
        costUsd: 4.1 * rounds,
        agentsStarted: rounds + 1,
        agentsEnded: rounds,
        jobsMeasured: 2 * rounds,
        objectives: [
          {
            metric: 'throughput',
            direction: ObjectiveDirection.MAXIMIZE,
            start: bestValue(1.2 + 0.1 * rounds, atMs - 40 * SECOND, `job-${rounds}a`),
            end: bestValue(1.3 + 0.2 * rounds, atMs - 10 * SECOND, `job-${rounds}b`),
          },
        ],
        plansCreated: Array.from({length: rounds}, (_, at) => 10 * rounds + at),
        plansChanged: rounds > 1 ? [rounds] : [],
      }),
    );
  const first = press(daemon, scenario, 'press-3', firstAt);
  settle(first, firstAt, 3);
  const second = press(daemon, scenario, 'press-2', secondAt);
  if (shape === 'history') daemon.failCheckIn(second, 'the job log is not answering');
  else settle(second, secondAt, 2);
  const third = press(daemon, scenario, 'press-1', thirdAt);
  settle(third, thirdAt, 1, 0);
  const latest = press(daemon, scenario, 'press-0', end);
  if (shape === 'pending') return;
  if (shape === 'failed') {
    daemon.failCheckIn(latest, 'the job log is not answering');
    return;
  }
  settle(latest, end, 4, 2);
  if (shape === 'written' || shape === 'history')
    daemon.postReport(latest, 'Best is up 0.47x, now 71% of the bound', REPORT);
}
