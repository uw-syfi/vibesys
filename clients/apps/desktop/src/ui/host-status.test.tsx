import {describe, expect, it} from '@effect/vitest';
import fc from 'fast-check';
import {
  emptyState,
  HOST_STATUS_KINDS,
  type HostStatus,
  initialNavigation,
  REFUSAL_CAUSES,
} from '../state/index.js';
import {installDom, mount} from '../testing/dom/harness.js';
import {CostScreen} from './cost/index.js';
import {hostStatusWord} from './host-status-words.js';
import {MachinesScreen} from './machines/index.js';
import {FleetScreen} from './orchestrator/fleet-screen.js';
import {COST_WORDS} from './test-display-clock.js';

installDom();

/** Every status a host can have, with a cause for each refusal and an age for a stale host. */
const statuses: fc.Arbitrary<HostStatus> = fc.oneof(
  fc.constantFrom(...HOST_STATUS_KINDS.filter(kind => kind !== 'refused')).map(kind => ({
    kind,
    cause: null,
    staleForMs: kind === 'stale' ? 3 * 60_000 : null,
  })),
  fc.constantFrom(null, ...REFUSAL_CAUSES).map(cause => ({
    kind: 'refused' as const,
    cause,
    staleForMs: null,
  })),
);

/** The words every status chip on screen says: what a screen's text says of a host. */
const chips = (screen: ReturnType<typeof mount>): string[] =>
  [...screen.container.querySelectorAll('.vs-chip')]
    .filter(chip => chip.closest('.vs-reserve') === null)
    .map(chip => chip.textContent ?? '');

/** The words a different status would say, which no screen may say of this one. */
const otherWords = (status: HostStatus): string[] =>
  HOST_STATUS_KINDS.filter(kind => kind !== status.kind).map(
    kind => hostStatusWord({kind, cause: null, staleForMs: null}).label,
  );

function settings(status: HostStatus) {
  return mount(<MachinesScreen rows={[{key: 'ssh:host-a', label: 'host-a', status}]} />);
}

function runs(status: HostStatus) {
  return mount(
    <FleetScreen
      hosts={[
        {
          key: 'ssh:host-a',
          label: 'host-a',
          data: emptyState,
          runIndex: {_tag: 'Ready', freshness: 'current', runs: new Map()},
          freshness: 'empty',
          status,
        },
      ]}
      place={initialNavigation.fleet}
      nowMs={0}
      go={() => undefined}
      refreshStates={{'ssh:host-a': {_tag: 'Idle'}}}
      refreshHost={() => undefined}
      searchStates={{'ssh:host-a': {_tag: 'Idle'}}}
      newRun={null}
    />,
  );
}

function cost(status: HostStatus) {
  return mount(
    <CostScreen
      view={undefined}
      status={status}
      feed={{_tag: 'Live'}}
      runOver={false}
      words={COST_WORDS}
      trail={[]}
      reading={null}
      retry={() => undefined}
    />,
  );
}

describe('a host status keeps its meaning on every screen', () => {
  it('uses the same word globally and scopes it to managed details in the disjoint Runs view', () => {
    fc.assert(
      fc.property(statuses, status => {
        const {label} = hostStatusWord(status);
        for (const open of [settings, cost]) {
          const screen = open(status);
          const said = chips(screen);
          expect(said).toContain(label);
          expect(
            said.filter(word => otherWords(status).includes(word)),
            open.name,
          ).toEqual([]);
          screen.unmount();
        }
        const list = runs(status);
        // Runs has two independent discovery sources, so a managed status cannot claim the host or
        // its generic index as a whole. Live is the quiet, healthy case.
        expect(chips(list)).toEqual(status.kind === 'live' ? [] : [`Managed details · ${label}`]);
        list.unmount();
      }),
      {numRuns: 60},
    );
  });

  it('has one status in the machine card header, with no separate Freshness', () => {
    const screen = settings({kind: 'stale', cause: null, staleForMs: 60_000});
    expect(chips(screen)).toEqual(['Stale 1m']);
    expect(screen.container.querySelectorAll('section.vs-card')).toHaveLength(1);
    expect(screen.text()).not.toContain('Freshness');
    screen.unmount();
  });
});
