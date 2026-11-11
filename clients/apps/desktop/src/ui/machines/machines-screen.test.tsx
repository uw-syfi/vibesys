import {create} from '@bufbuild/protobuf';
import {describe, expect, it} from '@effect/vitest';
import {
  EvalPlaceKind,
  EvalPlaceSchema,
  ListEvalPlacesResponseSchema,
  OffersUnreadableReason,
} from '@vibesys/orchestrator-proto';
import {
  failedOf,
  type MachineRow,
  type MachineView,
  NOT_READ,
  readingOf,
  readOf,
} from '../../state/index.js';
import {installDom, mount} from '../../testing/dom/harness.js';
import {richPlaces} from '../../testing/orchestrator/new-run-catalogue.js';
import {type MachineStatsHost, MachinesScreen, type PlacesHost} from './machines-screen.js';

installDom();

const GIB = 1024 ** 3;

const live: MachineRow = {
  key: 'local',
  label: 'This machine',
  status: {kind: 'live', cause: null, staleForMs: null},
};
const refused: MachineRow = {
  key: 'ssh:host-b',
  label: 'host-b',
  status: {kind: 'refused', cause: null, staleForMs: null},
};

const notProbed = create(EvalPlaceSchema, {
  name: 'cluster-c',
  kind: EvalPlaceKind.SLURM,
  state: {case: 'unreadable', value: {reason: OffersUnreadableReason.NOT_PROBEABLE}},
});
const response = create(ListEvalPlacesResponseSchema, {places: [...richPlaces(), notProbed]});

const sample: MachineView = {
  _tag: 'Shown',
  stale: null,
  sample: {
    cpuPercent: {_tag: 'Read', value: 37.5},
    memory: {_tag: 'Read', value: {usedBytes: 41 * GIB, totalBytes: 128 * GIB}},
    gpus: {
      _tag: 'Read',
      value: [
        {
          index: 0,
          name: 'H100',
          utilizationPercent: 92,
          memory: {usedBytes: 71 * GIB, totalBytes: 80 * GIB},
        },
      ],
    },
  },
};

function open(
  options: {
    readonly rows?: readonly MachineRow[];
    readonly places?: Readonly<Record<string, PlacesHost['places']>>;
    readonly stats?: Readonly<Record<string, MachineView>>;
  } = {},
) {
  const refreshed: string[] = [];
  const rows = options.rows ?? [live];
  const draw = (places: Readonly<Record<string, PlacesHost['places']>>) => (
    <MachinesScreen
      rows={rows}
      stats={rows.map(
        (row): MachineStatsHost => ({
          key: row.key,
          label: row.label,
          view: options.stats?.[row.key] ?? {_tag: 'Waiting'},
        }),
      )}
      places={{
        hosts: rows.map(row => ({
          key: row.key,
          label: row.label,
          places: places[row.key] ?? NOT_READ,
        })),
        refresh: key => refreshed.push(key),
      }}
    />
  );
  const screen = mount(draw(options.places ?? {}));
  return {screen, refreshed, draw};
}

const bodyRows = (container: HTMLElement) => [
  ...container.querySelectorAll<HTMLElement>('table[aria-label="Machines"] tbody tr'),
];

/** Each row as the person reads it: its kind and the text of each cell. */
const cells = (container: HTMLElement): string[][] =>
  bodyRows(container).map(row => [
    row.getAttribute('data-kind') ?? 'machine',
    ...[...row.querySelectorAll('td')].map(cell => cell.textContent?.trim() ?? ''),
  ]);

describe('the Machines page', () => {
  it('is one table: the machine, then each place it can run jobs at, in the host’s order', () => {
    const {screen} = open({places: {local: readOf(response)}, stats: {local: sample}});
    expect(screen.container.querySelectorAll('table')).toHaveLength(1);
    expect(cells(screen.container).map(row => row.slice(0, 2))).toEqual([
      ['machine', 'This machine'],
      ['sub', 'local'],
      ['sub', 'host-a'],
      ['sub', 'cluster-a'],
      ['sub', 'cluster-b'],
      ['sub', 'cluster-c'],
    ]);
    screen.unmount();
  });

  it('gives the machine its status and readings, and each place its reach and what it offers', () => {
    const {screen} = open({places: {local: readOf(response)}, stats: {local: sample}});
    const rows = cells(screen.container);
    expect(rows[0]).toEqual([
      'machine',
      'This machine',
      'Live',
      '37.5%',
      '41 / 128 GiB',
      '92%',
      '',
    ]);
    expect(
      rows.slice(1).map(row => row.slice(1, 2).concat(row.slice(2, 3), row.slice(5, 6))),
    ).toEqual([
      ['local', 'Reachable', 'CPU only'],
      ['host-a', 'Reachable', '8× H100'],
      ['cluster-a', 'Reachable', '8× MI300X'],
      ['cluster-b', 'Unreachable', ''],
      ['cluster-c', 'Not probed', ''],
    ]);
    screen.unmount();
  });

  it('sizes a status chip to its word and names a machine whole on hover, never wrapped', () => {
    const {screen} = open({places: {local: readOf(response)}});
    for (const chip of screen.container.querySelectorAll('.vs-chip')) {
      expect(chip.hasAttribute('data-even')).toBe(false);
    }
    const name = screen.container.querySelector<HTMLElement>('td[data-clip]');
    expect(name?.getAttribute('title')).toBe('This machine');
    screen.unmount();
  });

  it('says what the readings and places wait for in the same rows, so nothing moves as they come', () => {
    const {screen} = open({rows: [live, refused]});
    expect(cells(screen.container)).toEqual([
      ['machine', 'This machine', 'Live', 'Waiting', '', '', ''],
      ['sub', 'Waiting for job places', '', '', '', '', ''],
      ['machine', 'host-b', 'Refused', 'Waiting', '', '', ''],
      ['sub', 'Waiting for job places', '', '', '', '', ''],
    ]);
    screen.unmount();
  });

  it('says No stats with a hint, and why places were not read, for a machine that cannot say', () => {
    const {screen} = open({
      rows: [refused],
      stats: {'ssh:host-b': {_tag: 'Missing', why: 'unsupported'}},
      places: {'ssh:host-b': failedOf(NOT_READ, 'host unreachable')},
    });
    const rows = cells(screen.container);
    expect(rows[0]?.[3]).toBe('No stats');
    expect(rows[1]).toEqual(['sub', 'Job places not read', 'Not read', '', '', '', '']);
    const hint = screen.container.querySelector('.vs-chip[aria-description]');
    expect(hint?.getAttribute('aria-description')).toBe('host unreachable');
    screen.unmount();
  });

  it('shows an unreachable place’s reason as the hint of its chip', () => {
    const reason = create(EvalPlaceSchema, {
      name: 'cluster-b',
      kind: EvalPlaceKind.SLURM,
      state: {
        case: 'unreadable',
        value: {reason: OffersUnreadableReason.UNREACHABLE, detail: 'connection timed out'},
      },
    });
    const answer = readOf(create(ListEvalPlacesResponseSchema, {places: [reason]}));
    const {screen} = open({places: {local: answer}});
    const chip = [...screen.container.querySelectorAll('.vs-chip')].find(
      each => each.textContent === 'Unreachable',
    );
    expect(chip?.getAttribute('aria-description')).toBe('connection timed out');
    screen.unmount();
  });

  it('says in one line how many machines, how many are live, how many places and how many cannot be reached', () => {
    const {screen} = open({rows: [live, refused], places: {local: readOf(response)}});
    expect(screen.container.querySelector('.vs-factline')?.textContent).toBe(
      '2 machines·1 live·5 job places·1 unreachable·places not read on 1',
    );
    screen.unmount();
  });

  it('picks no place row, and shows an empty list as one quiet row', () => {
    const {screen} = open({places: {local: readOf(response)}});
    expect(
      [...screen.container.querySelectorAll('tr[data-kind="sub"]')].filter(each =>
        each.hasAttribute('data-selectable'),
      ),
    ).toHaveLength(0);
    screen.unmount();
    const none = mount(<MachinesScreen rows={[]} />);
    expect(cells(none.container)).toEqual([['empty', 'No machines']]);
    none.unmount();
  });
});

describe('Refresh of a machine’s places', () => {
  it('asks the host to probe again, and offers no editing', () => {
    const {screen, refreshed} = open({places: {local: readOf(response)}});
    screen.press(screen.control('Refresh This machine'));
    expect(refreshed).toEqual(['local']);
    expect(
      screen.container.querySelectorAll('table input, table select, table textarea'),
    ).toHaveLength(0);
    screen.unmount();
  });

  const dimmed = (container: HTMLElement): number =>
    container.querySelectorAll('tr[data-kind="sub"] .vs-stale[data-stale]').length;

  it('does not dim the places for the read that opening the page starts, and swaps once to the answer', () => {
    const {screen, draw} = open({places: {local: readOf(response)}});
    screen.render(draw({local: readingOf(readOf(response))}));
    expect(dimmed(screen.container)).toBe(0);
    expect(cells(screen.container)).toHaveLength(6);
    const answer = create(ListEvalPlacesResponseSchema, {places: [notProbed]});
    screen.render(draw({local: readOf(answer)}));
    expect(dimmed(screen.container)).toBe(0);
    expect(cells(screen.container).map(row => row[1])).toEqual(['This machine', 'cluster-c']);
    screen.unmount();
  });

  it('dims that machine’s places while the read the person asked for goes on, and swaps once to the answer', () => {
    const {screen, draw} = open({places: {local: readOf(response)}});
    screen.press(screen.control('Refresh This machine'));
    // The host's answer to the press is not in yet: nothing is dimmed before the read begins.
    expect(dimmed(screen.container)).toBe(0);
    screen.render(draw({local: readingOf(readOf(response))}));
    // Each place dims its name, its reach and its offers.
    expect(dimmed(screen.container)).toBe(5 * 3);
    expect(cells(screen.container)).toHaveLength(6);
    expect(screen.control('Refresh This machine').getAttribute('aria-disabled')).toBe('true');
    const answer = create(ListEvalPlacesResponseSchema, {places: [notProbed]});
    screen.render(draw({local: readOf(answer)}));
    expect(dimmed(screen.container)).toBe(0);
    expect(cells(screen.container).map(row => row[1])).toEqual(['This machine', 'cluster-c']);
    screen.unmount();
  });

  it('dims only the machine asked about', () => {
    const rows = [live, refused];
    const both = {local: readOf(response), 'ssh:host-b': readOf(response)};
    const {screen, draw} = open({rows, places: both});
    screen.press(screen.control('Refresh This machine'));
    screen.render(draw({...both, local: readingOf(readOf(response))}));
    expect(dimmed(screen.container)).toBe(5 * 3);
    screen.unmount();
  });
});
