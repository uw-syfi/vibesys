/** A saved run's committed result outranks every previous registry tombstone. */
import {expect, test} from 'bun:test';
import type {HostRun, RunDigestEntry} from '@vibesys/hosts';
import {digestKey, recentRows, SIDEBAR_EMPTY, type SidebarState} from './index.js';

const RUN = {host: 'local' as const, runId: 'run-a'};
const OUTCOMES = ['adopted', 'failed', 'stopped', null] as const;
const STATUS = {adopted: 'finished', failed: 'failed', stopped: 'stopped'} as const;

for (const stored of OUTCOMES) {
  for (const registered of OUTCOMES) {
    test(`committed ${stored} outranks registry ${registered}`, () => {
      const ended: HostRun = {
        id: 'old',
        runId: RUN.runId,
        projectRoot: '/project',
        status: 'failed',
        phase: 'failed',
        startedAt: 0,
        paused: null,
        reason: null,
        end: {outcome: registered, reason: 'outdated registry reason', resumable: true},
      };
      const entry: RunDigestEntry = {
        kind: 'read',
        createdAt: 1,
        ideas: 1,
        adoptedRatio: 5.17,
        bestSoFarRatio: null,
        durationSeconds: 3600,
        outcome: stored,
        closing:
          stored === null
            ? null
            : stored === 'failed'
              ? 'failed'
              : stored === 'stopped'
                ? 'interrupted'
                : 'finished',
        over: stored !== null,
        endSummary: 'Committed result',
      };
      const state: SidebarState = {
        ...SIDEBAR_EMPTY,
        recent: [
          {
            ...RUN,
            hostLabel: 'Local',
            project: '/project',
            task: null,
            instanceId: null,
            attachedAt: 1,
          },
        ],
        listings: [
          {kind: 'listed', host: 'local', live: [], ended: [ended], unreadable: [], unverified: []},
        ],
        digests: {[digestKey(RUN)]: {kind: 'read', entry}},
      };
      const row = recentRows(state).rows[0];
      expect(row?.status).toBe(stored === null ? 'stopped' : STATUS[stored]);
      expect(row?.title).toBe('project');
      expect(row?.tooltip).not.toContain('outdated registry reason');
      if (stored !== null) expect(row?.tooltip).toContain('Committed result');
    });
  }
}
