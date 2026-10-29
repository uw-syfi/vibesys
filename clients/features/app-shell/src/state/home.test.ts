import {expect, test} from 'bun:test';
import type {HostListing} from '@vibesys/hosts';
import {runsKnown, SIDEBAR_EMPTY, type SidebarState} from './index.js';

const listed = (host: string): HostListing =>
  ({kind: 'listed', host, live: [], ended: [], unreadable: [], unverified: []}) as HostListing;

const heard: SidebarState = {...SIDEBAR_EMPTY, overviewHeard: true, listings: [listed('local')]};

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
