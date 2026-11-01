import {expect, test} from 'bun:test';
import type {HostListing} from '@vibesys/hosts';
import fc from 'fast-check';
import {renderToStaticMarkup} from 'react-dom/server';
import {SIDEBAR_EMPTY, type SidebarState} from '../../state/index.js';
import {AppShell} from './app-shell.js';

const LISTINGS: readonly HostListing[] = [
  {kind: 'listed', host: 'local', live: [], ended: [], unreadable: [], unverified: []},
  {kind: 'unavailable', host: 'local', step: 'gateway', connection: {kind: 'connecting'}},
  {
    kind: 'unavailable',
    host: 'ssh:host-a',
    step: 'runs',
    connection: {kind: 'reconnecting', attempt: 2},
  },
  {
    kind: 'unavailable',
    host: 'ssh:host-a',
    step: 'handshake',
    connection: {kind: 'offline', detail: 'gateway startup exited: 1'},
  },
];

/** The rail's destinations, in order, as `state` renders them. */
function railEntries(state: SidebarState, home: 'new-run' | 'runs'): string[] {
  const html = renderToStaticMarkup(
    <AppShell
      state={state}
      settled="ready"
      dispatch={() => {}}
      badges={{needsYou: 0, machineWarning: null}}
      home={{kind: home}}
      version="test"
      screen={() => null}
    />,
  );
  return [...html.matchAll(/class="vs-rail-item"[^>]* title="([^"]+)"/g)].map(
    match => match[1] ?? '',
  );
}

test('property: the rail lists the same entries in every listing state', () => {
  const expected = railEntries(SIDEBAR_EMPTY, 'runs');
  expect(expected).toEqual(['VibeSys test', 'New run', 'Runs', 'Machines', 'Usage', 'Settings']);
  fc.assert(
    fc.property(
      fc.subarray([...LISTINGS]),
      fc.constantFrom<SidebarState['overview']>(
        {kind: 'waiting'},
        {kind: 'heard'},
        {kind: 'failed', reason: 'down'},
      ),
      fc.boolean(),
      fc.constantFrom('new-run' as const, 'runs' as const),
      (listings, overview, runsSeen, home) => {
        const state = {...SIDEBAR_EMPTY, listings, overview, runsSeen};
        expect(railEntries(state, home)).toEqual(expected);
      },
    ),
  );
});
