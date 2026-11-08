import {create} from '@bufbuild/protobuf';
import {describe, expect, it} from '@effect/vitest';
import {
  EvalPlaceKind,
  EvalPlaceSchema,
  ListEvalPlacesResponseSchema,
  OffersUnreadableReason,
} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';
import {failedOf, NOT_READ, reachOf, readingOf, readOf} from '../../state/index.js';
import {installDom, mount} from '../../testing/dom/harness.js';
import {richPlaces} from '../../testing/orchestrator/new-run-catalogue.js';
import {MachinesScreen} from './machines-screen.js';
import type {PlacesHost} from './places-panel.js';

installDom();

const notProbed = create(EvalPlaceSchema, {
  name: 'cluster-c',
  kind: EvalPlaceKind.SLURM,
  state: {case: 'unreadable', value: {reason: OffersUnreadableReason.NOT_PROBEABLE}},
});

const response = create(ListEvalPlacesResponseSchema, {places: [...richPlaces(), notProbed]});

function open(places: PlacesHost['places']) {
  const refreshed: string[] = [];
  const hosts = (now: PlacesHost['places']): PlacesHost[] => [
    {key: 'local', label: 'This machine', places: now},
  ];
  const draw = (now: PlacesHost['places']) => (
    <MachinesScreen
      rows={[
        {
          key: 'local',
          label: 'This machine',
          status: {kind: 'live', cause: null, staleForMs: null},
        },
      ]}
      places={{hosts: hosts(now), refresh: key => refreshed.push(key)}}
    />
  );
  const screen = mount(draw(places));
  return {screen, refreshed, draw};
}

const cells = (container: HTMLElement): string[][] =>
  [...container.querySelectorAll('table')]
    .filter(each => each.getAttribute('aria-label') === 'This machine places')
    .flatMap(table => [...table.querySelectorAll('tbody tr')])
    .map(row => [...row.querySelectorAll('td')].map(cell => cell.textContent?.trim() ?? ''));

describe('the eval places of a machine', () => {
  it('lists each place with its reach and offers, in the host’s order', () => {
    const {screen} = open(readOf(response));
    expect(cells(screen.container)).toEqual([
      ['local', 'reachable', ''],
      ['host-a', 'reachable', '8× H100'],
      ['cluster-a', 'reachable', '8× MI300X'],
      ['cluster-b', 'unreachable', ''],
      ['cluster-c', 'not probed', ''],
    ]);
    screen.unmount();
  });

  it('asks the host to probe again from the machine’s Refresh, and offers no editing', () => {
    const {screen, refreshed} = open(readOf(response));
    screen.press(screen.control('Refresh This machine'));
    expect(refreshed).toEqual(['local']);
    expect(
      screen.container.querySelectorAll('table input, table select, table textarea'),
    ).toHaveLength(0);
    screen.unmount();
  });

  it('dims the places while they are read again, and swaps once to the answer', () => {
    const {screen, draw} = open(readOf(response));
    const dimmed = () => screen.container.querySelector('.vs-dim')?.hasAttribute('data-dim');
    expect(dimmed()).toBe(false);
    screen.render(draw(readingOf(readOf(response))));
    expect(dimmed()).toBe(true);
    expect(cells(screen.container)).toHaveLength(5);
    expect(screen.control('Refresh This machine').getAttribute('aria-disabled')).toBe('true');
    const answer = create(ListEvalPlacesResponseSchema, {places: [notProbed]});
    screen.render(draw(readOf(answer)));
    expect(dimmed()).toBe(false);
    expect(cells(screen.container)).toEqual([['cluster-c', 'not probed', '']]);
    screen.unmount();
  });

  it('keeps the status slot in the header whether or not a read failed', () => {
    const {screen, draw} = open(readOf(response));
    const slots = () =>
      [...screen.container.querySelectorAll('.vs-card *')].filter(
        each => each.children.length === 0 && each.textContent === 'Not read',
      );
    expect(slots()).toHaveLength(1);
    expect(slots()[0]?.closest('[aria-hidden="true"]')).not.toBeNull();
    screen.render(draw(failedOf(readOf(response), 'host unreachable')));
    expect(slots()).toHaveLength(1);
    expect(slots()[0]?.closest('[aria-hidden="true"]')).toBeNull();
    expect(cells(screen.container)).toHaveLength(5);
    screen.unmount();
  });

  it('shows no places before the first read', () => {
    const {screen} = open(NOT_READ);
    expect(cells(screen.container)).toEqual([]);
    screen.unmount();
  });
});

describe('the reach of a place', () => {
  it('is unreachable exactly when the probe could not connect or timed out', () => {
    const reasons = Object.values(OffersUnreadableReason).filter(
      (value): value is OffersUnreadableReason => typeof value === 'number',
    );
    fc.assert(
      fc.property(fc.constantFrom(...reasons), reason => {
        const place = create(EvalPlaceSchema, {
          name: 'p',
          kind: EvalPlaceKind.SSH,
          state: {case: 'unreadable', value: {reason}},
        });
        const expected =
          reason === OffersUnreadableReason.UNREACHABLE ||
          reason === OffersUnreadableReason.TIMED_OUT
            ? 'unreachable'
            : 'not probed';
        expect(reachOf(place)).toBe(expected);
      }),
    );
  });
});
