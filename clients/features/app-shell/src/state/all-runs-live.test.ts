import {describe, expect, test} from 'bun:test';
import type {HostListing, HostRun, RunDigestEntry, RunDigestsReply} from '@vibesys/hosts';
import {
  initialSidebar,
  type SidebarAction,
  type SidebarRequest,
  type SidebarState,
  sidebarStep,
} from './index.js';

const HOST = 'ssh:host-b';
const PROJECT = '~/projects/queue-rs';
const NOW = Date.parse('2026-10-17T12:00:00Z');

function hostRun(runId: string, phase: 'running' | 'completed'): HostRun {
  return {
    id: '00000000000a',
    projectRoot: PROJECT,
    runId,
    status: phase,
    phase,
    paused: null,
    end: null,
    startedAt: NOW / 1000,
    reason: null,
  };
}

function read(): RunDigestEntry {
  return {
    kind: 'read',
    createdAt: NOW,
    ideas: 3,
    adoptedRatio: null,
    bestSoFarRatio: 1.5,
    durationSeconds: 60,
    outcome: null,
    closing: null,
    over: false,
  };
}

/** The ways a host can answer a digest request. */
const REPLIES: readonly ((runIds: readonly string[]) => RunDigestsReply | null)[] = [
  runIds => ({kind: 'digests', runs: new Map(runIds.map(id => [id, read()]))}),
  () => ({kind: 'digests', runs: new Map()}),
  () => ({kind: 'unsupported'}),
  () => null,
];

/**
 * Answers every request the sidebar makes (listings with `listed`, digests with `reply`) until it
 * asks for nothing more; returns the digest requests made and whether it settled within `rounds`.
 */
function settle(
  start: SidebarState,
  requests: readonly SidebarRequest[],
  listed: readonly string[],
  reply: (typeof REPLIES)[number],
  rounds = 20,
): {digestRequests: number; settled: boolean} {
  let state = start;
  let pending = requests;
  let digestRequests = 0;
  for (let round = 0; round < rounds && pending.length > 0; round += 1) {
    const next: SidebarRequest[] = [];
    for (const request of pending) {
      let action: SidebarAction;
      if (request.kind === 'cancel-project-runs') continue;
      if (request.kind === 'read-digests') {
        digestRequests += 1;
        action = {type: 'digests-read', token: request.token, reply: reply(request.runIds)};
      } else {
        action = {
          type: 'project-runs-listed',
          token: request.token,
          host: request.host,
          project: request.project,
          runs: listed.map(runId => ({runId, name: runId, task: 'mpmc', createdAt: NOW})),
        };
      }
      const step = sidebarStep(state, action);
      state = step.state;
      next.push(...step.requests);
    }
    pending = next;
  }
  return {digestRequests, settled: pending.length === 0};
}

describe('the dashboard with live runs', () => {
  test('regression: answered requests settle, and each run is asked for once', () => {
    for (const liveCount of [1, 2, 3]) {
      for (const endedCount of [0, 1, 4]) {
        for (const reply of REPLIES) {
          const live = Array.from({length: liveCount}, (_, i) => `live-${i}`);
          const ended = Array.from({length: endedCount}, (_, i) => `ended-${i}`);
          const listing: HostListing = {
            kind: 'listed',
            host: HOST,
            live: live.map(id => hostRun(id, 'running')),
            ended: ended.map(id => hostRun(id, 'completed')),
            unreadable: [],
            unverified: [],
          };
          const first = sidebarStep(initialSidebar().state, {type: 'listing', listing});
          const {digestRequests, settled} = settle(
            first.state,
            first.requests,
            [...live, ...ended],
            reply,
          );
          expect(settled, `${liveCount} live, ${endedCount} ended`).toBe(true);
          // Each live run once, for its row's figures; each ended run once, for the dashboard's tile.
          expect(digestRequests).toBeLessThanOrEqual(liveCount + endedCount);
        }
      }
    }
  });
});
