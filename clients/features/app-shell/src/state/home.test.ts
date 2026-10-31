import {expect, test} from 'bun:test';
import type {HostListing} from '@vibesys/hosts';
import {machineLoads, runsKnown, SIDEBAR_EMPTY, type SidebarState} from './index.js';

const listed = (host: string): HostListing =>
  ({kind: 'listed', host, live: [], ended: [], unreadable: [], unverified: []}) as HostListing;

const heard: SidebarState = {
  ...SIDEBAR_EMPTY,
  overview: {kind: 'heard'},
  listings: [listed('local')],
};

test('nothing is known before the overview, or while a machine has not listed', () => {
  expect(runsKnown(SIDEBAR_EMPTY, [])).toBe('unknown');
  expect(runsKnown(heard, null)).toBe('unknown');
  expect(runsKnown(heard, ['local', 'ssh:host-a'])).toBe('unknown');
});

test('no run is known once every machine has listed and nothing is loading', () => {
  expect(runsKnown(heard, ['local'])).toBe('none');
  expect(runsKnown(heard, [])).toBe('none');
});

test('a project whose runs are still loading keeps the answer open', () => {
  const loading: SidebarState = {
    ...heard,
    allRuns: {
      ...heard.allRuns,
      projects: {[JSON.stringify(['local', '/p'])]: {kind: 'loading', token: 1}},
    },
  };
  expect(runsKnown(loading, ['local'])).toBe('unknown');
});

test('a run listed by any machine is known at once, before the others have answered', () => {
  const live = {
    ...listed('ssh:host-a'),
    live: [{id: 'a', projectRoot: '/p', runId: 'r1', status: 'running', phase: 'running'}],
  } as unknown as HostListing;
  expect(runsKnown({...SIDEBAR_EMPTY, listings: [live]}, ['local', 'ssh:host-a'])).toBe('some');
});

test('a machine is loading, not listed, until the projects it lists are read', () => {
  const key = JSON.stringify(['local', '/p']);
  const reading: SidebarState = {
    ...heard,
    allRuns: {...heard.allRuns, projects: {[key]: {kind: 'loading', token: 1}}},
  };
  expect(machineLoads(heard, ['local'])).toEqual([{kind: 'listed', host: 'local'}]);
  expect(machineLoads(reading, ['local'])).toEqual([
    {kind: 'loading', host: 'local', step: 'runs', attempt: 0},
  ]);
});

test('a project asked again after failing does not reopen its machine', () => {
  const refusal = {kind: 'unreachable', detail: 'down'} as const;
  const again: SidebarState = {
    ...heard,
    allRuns: {
      ...heard.allRuns,
      projects: {[JSON.stringify(['local', '/p'])]: {kind: 'loading', token: 2, refusal}},
    },
  };
  expect(machineLoads(again, ['local'])).toEqual([{kind: 'listed', host: 'local'}]);
});

test('a failed overview fails the machines it would have listed, not those already heard', () => {
  const state: SidebarState = {
    ...heard,
    overview: {kind: 'failed', reason: 'Error: the main process went away'},
  };
  expect(machineLoads(state, ['local', 'ssh:host-a'])).toEqual([
    {kind: 'listed', host: 'local'},
    {
      kind: 'failed',
      host: 'ssh:host-a',
      step: 'gateway',
      reason: 'the main process went away',
    },
  ]);
});

test('with the saved machines unread This Mac still has a load, then what has listed', () => {
  expect(machineLoads(SIDEBAR_EMPTY, null).map(load => load.host)).toEqual(['local']);
  const state = {...SIDEBAR_EMPTY, listings: [listed('ssh:host-a')]};
  expect(machineLoads(state, null).map(load => load.host)).toEqual(['local', 'ssh:host-a']);
});

test('a removed machine keeps no load, whatever its last listing said', () => {
  const stale: HostListing = {
    kind: 'unavailable',
    host: 'ssh:host-a',
    step: 'gateway',
    connection: {kind: 'offline', detail: 'down'},
  };
  const state = {...heard, listings: [...heard.listings, stale]};
  expect(machineLoads(state, ['local'])).toEqual([{kind: 'listed', host: 'local'}]);
});
