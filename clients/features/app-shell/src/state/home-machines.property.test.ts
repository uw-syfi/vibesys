import {expect, test} from 'bun:test';
import type {ConnectionStatus, HostKey, HostListing} from '@vibesys/hosts';
import fc from 'fast-check';
import {runsKnown, SIDEBAR_EMPTY, type SidebarState} from './index.js';

/** How a machine stands at one moment: not heard from, on its way, stopped, or listing no runs. */
type Standing = 'absent' | 'connecting' | 'connected' | 'reconnecting' | 'offline' | 'listed';

const STANDINGS: readonly Standing[] = [
  'absent',
  'connecting',
  'connected',
  'reconnecting',
  'offline',
  'listed',
];

/** Whether a machine at `standing` is still on its way to an answer. */
const answering = (standing: Standing): boolean => standing !== 'offline' && standing !== 'listed';

const CONNECTIONS: Readonly<
  Record<'connecting' | 'connected' | 'reconnecting' | 'offline', ConnectionStatus>
> = {
  connecting: {kind: 'connecting'},
  connected: {kind: 'connected'},
  reconnecting: {kind: 'reconnecting', attempt: 2},
  offline: {kind: 'offline', detail: 'gateway startup exited: 1'},
};

function listingOf(host: HostKey, standing: Standing): HostListing[] {
  if (standing === 'absent') return [];
  if (standing === 'listed') {
    return [{kind: 'listed', host, live: [], ended: [], unreadable: [], unverified: []}];
  }
  return [{kind: 'unavailable', host, step: 'handshake', connection: CONNECTIONS[standing]}];
}

/** Machines in a given order, each at a standing, whose listings arrive in `arrival` order. */
const scene = fc
  .array(fc.constantFrom(...STANDINGS), {minLength: 1, maxLength: 5})
  .chain(standings =>
    fc.record({
      standings: fc.constant(standings),
      arrival: fc.shuffledSubarray(
        standings.map((_, at) => at),
        {minLength: standings.length, maxLength: standings.length},
      ),
    }),
  );

function stateOf(standings: readonly Standing[], arrival: readonly number[]): SidebarState {
  return {
    ...SIDEBAR_EMPTY,
    overview: {kind: 'heard'},
    listings: arrival.flatMap(at => listingOf(`ssh:host-${at}`, standings[at] as Standing)),
  };
}

test('property: home decides on no run only once no machine is still answering, in any order', () => {
  fc.assert(
    fc.property(scene, ({standings, arrival}) => {
      const machines = standings.map((_, at): HostKey => `ssh:host-${at}`);
      const known = runsKnown(stateOf(standings, arrival), machines);
      expect(known).toBe(standings.some(answering) ? 'unknown' : 'none');
    }),
  );
});

test('property: a machine answering later never moves home to New run in between', () => {
  fc.assert(
    fc.property(scene, ({standings, arrival}) => {
      const machines = standings.map((_, at): HostKey => `ssh:host-${at}`);
      // Machines are heard one at a time; at every prefix of the arrivals, `none` needs all heard.
      for (let heard = 0; heard <= arrival.length; heard += 1) {
        const present = arrival.slice(0, heard);
        const partial = standings.map((each, at) => (present.includes(at) ? each : 'absent'));
        const known = runsKnown(stateOf(partial, present), machines);
        if (known === 'none') expect(partial.some(answering)).toBe(false);
      }
    }),
  );
});
