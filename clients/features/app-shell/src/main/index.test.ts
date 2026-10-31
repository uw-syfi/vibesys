import {expect, test} from 'bun:test';
import type {HostListing, HostRun} from '@vibesys/hosts';
import type {RunNotice} from '../state/index.js';
import {RunNotifier} from './index.js';

function server(phase: HostRun['phase']): HostRun {
  return {
    id: '000000000001',
    projectRoot: '/p/queue-rs',
    runId: '20261015-163047',
    status: phase,
    phase,
    paused: phase === 'paused' ? 'user' : null,
    end:
      phase === 'completed'
        ? {outcome: 'adopted', reason: 'kept the best', resumable: false}
        : null,
    startedAt: 1,
    reason: null,
  };
}

function listed(phase: HostRun['phase'], ended: boolean): HostListing {
  return {
    kind: 'listed',
    host: 'local',
    live: ended ? [] : [server(phase)],
    ended: ended ? [server(phase)] : [],
    unreadable: [],
    unverified: [],
  };
}

test('notifies only while enabled, and not again after the host was unavailable', () => {
  const shown: RunNotice[] = [];
  let enabled = false;
  const notifier = new RunNotifier({enabled: () => enabled, notify: notice => shown.push(notice)});
  notifier.listing(listed('running', false));
  notifier.listing(listed('paused', false));
  expect(shown).toEqual([]);
  enabled = true;
  notifier.listing({
    kind: 'unavailable',
    host: 'local',
    step: 'gateway',
    connection: {kind: 'offline', detail: 'no route'},
  });
  notifier.listing(listed('completed', true));
  expect(shown.map(notice => notice.kind)).toEqual(['finished']);
  notifier.listing(listed('completed', true));
  expect(shown).toHaveLength(1);
});
