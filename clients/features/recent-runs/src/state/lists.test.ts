import {describe, expect, test} from 'bun:test';
import type {HostListing} from '@vibesys/hosts';
import type {RecentRun} from '@vibesys/runs';
import {checkModel, type Seeded} from '@vibesys/store/testing';
import {LISTS_EMPTY, type ListsAction, listsInvariants, listsStep, statusOf} from './lists.js';

function run(host: string, runId: string): RecentRun {
  return {
    host,
    hostLabel: host,
    project: '/p',
    task: 'spsc',
    runId,
    instanceId: null,
    attachedAt: 1,
  };
}

const RUNS = [run('local', 'run-1'), run('ssh:gpu', 'run-2')];

function listing(host: string, live: string[], ended: string[]): HostListing {
  const entry = (runId: string, index: number) => ({
    id: `00000000000${index}`,
    projectRoot: '/p',
    runId,
    status: 'running',
    phase: 'running' as const,
    paused: null,
    end: null,
    startedAt: 1,
    reason: null,
  });
  return {
    kind: 'listed',
    host,
    live: live.map(entry),
    ended: ended.map(entry),
    unreadable: [],
    unverified: [],
  };
}

function randomListsAction(random: Seeded): ListsAction {
  const host = random.pick(['local', 'ssh:gpu']);
  return random.pick<ListsAction>([
    {type: 'runs', runs: RUNS.filter(() => random.bool())},
    {
      type: 'listing',
      listing: listing(host, random.bool() ? ['run-1'] : [], random.bool() ? ['run-2'] : []),
    },
    {
      type: 'listing',
      listing: {kind: 'unavailable', host, connection: {kind: 'reconnecting', attempt: 1}},
    },
  ]);
}

describe('lists', () => {
  test('model: listings fold from pushes only, and a run is live exactly when its host lists it', () => {
    checkModel({
      initial: LISTS_EMPTY,
      step: listsStep,
      action: random => randomListsAction(random),
      issues: () => null,
      awaited: () => ({}),
      reply: () => null,
      invariants: listsInvariants,
      also: (_before, _action, after, requests) => {
        expect(requests).toEqual([]);
        for (const recent of after.runs) {
          const shown = after.listings[recent.host];
          const live =
            shown?.kind === 'listed' && shown.live.some(entry => entry.runId === recent.runId);
          expect(statusOf(after, recent).kind === 'live').toBe(live);
        }
      },
    });
  });
});
