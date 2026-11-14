import {create} from '@bufbuild/protobuf';
import {describe, expect, it} from '@effect/vitest';
import {
  EvalPlaceKind,
  EvalPlaceSchema,
  ListEvalPlacesResponseSchema,
  OffersSource,
  OffersUnreadableReason,
} from '@vibesys/orchestrator-proto';
import type {ReactNode} from 'react';
import {
  emptyAddMachine,
  failedOf,
  type MachineRow,
  type MachineView,
  NOT_READ,
  readingOf,
  readOf,
} from '../../state/index.js';
import {installDom, mount} from '../../testing/dom/harness.js';
import {richPlaces} from '../../testing/orchestrator/new-run-catalogue.js';
import type {MachineControls} from './add-machine/machine-controls.js';
import {type MachineStatsHost, MachinesScreen, type PlacesHost} from './machines-screen.js';

installDom();

const GIB = 1024 ** 3;

const local: MachineRow = {
  key: 'local',
  label: 'This machine',
  status: {kind: 'live', cause: null, staleForMs: null},
};

const remote: MachineRow = {
  key: 'ssh:operator@host-a',
  label: 'operator@host-a',
  status: {kind: 'stale', cause: null, staleForMs: 120_000},
};

const refused: MachineRow = {
  key: 'ssh:host-b',
  label: 'host-b',
  status: {kind: 'refused', cause: null, staleForMs: null},
};

const sample = (cpuPercent: number): MachineView => ({
  _tag: 'Shown',
  stale: null,
  sample: {
    cpuPercent: {_tag: 'Read', value: cpuPercent},
    memory: {_tag: 'Read', value: {usedBytes: 8 * GIB, totalBytes: 32 * GIB}},
    gpus: {_tag: 'Read', value: []},
  },
});

const notProbed = create(EvalPlaceSchema, {
  name: 'cluster-c',
  kind: EvalPlaceKind.SLURM,
  state: {case: 'unreadable', value: {reason: OffersUnreadableReason.NOT_PROBEABLE}},
});

const response = create(ListEvalPlacesResponseSchema, {places: [...richPlaces(), notProbed]});

const directPlace = (name: string) =>
  create(EvalPlaceSchema, {
    name,
    kind: EvalPlaceKind.SSH,
    state: {case: 'known', value: {source: OffersSource.PROBED}},
  });

const controls = (remove: (key: string) => void, toasts: ReactNode = null): MachineControls => ({
  add: {
    state: emptyAddMachine,
    type: () => undefined,
    pick: () => undefined,
    submit: () => undefined,
    reset: () => undefined,
  },
  remove,
  toasts,
  selected: null,
  select: () => undefined,
});

const card = (container: HTMLElement, title: string): HTMLElement => {
  const found = [...container.querySelectorAll<HTMLElement>('section.vs-card')].find(section =>
    section.querySelector('.vs-card-title')?.textContent?.includes(title),
  );
  if (found === undefined) throw new Error(`no card named ${title}`);
  return found;
};

function open(
  options: {
    readonly rows?: readonly MachineRow[];
    readonly places?: Readonly<Record<string, PlacesHost['places']>>;
    readonly stats?: Readonly<Record<string, MachineView>>;
  } = {},
) {
  const refreshed: string[] = [];
  const rows = options.rows ?? [local];
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

const cells = (container: HTMLElement, label: string): string[][] =>
  [...container.querySelectorAll(`table[aria-label="${label}"] tbody tr`)].map(row =>
    [...row.querySelectorAll('td')].map(cell => cell.textContent?.trim() ?? ''),
  );

const dimmed = (container: HTMLElement, label: string): number =>
  container.querySelectorAll(`table[aria-label="${label}"] .vs-stale[data-stale]`).length;

describe('the Machines operator page', () => {
  it('joins connection, repository, telemetry and eval places by host key into professional cards', () => {
    const refreshed: string[] = [];
    const removed: string[] = [];
    const stats: readonly MachineStatsHost[] = [
      {key: remote.key, label: remote.label, view: sample(73)},
      {key: local.key, label: local.label, view: sample(11)},
    ];
    const places: readonly PlacesHost[] = [
      {
        key: remote.key,
        label: remote.label,
        places: readOf(
          create(ListEvalPlacesResponseSchema, {places: [directPlace('gpu-cluster')]}),
        ),
      },
      {
        key: local.key,
        label: local.label,
        places: readOf(create(ListEvalPlacesResponseSchema, {places: [directPlace('local')]})),
      },
    ];
    const screen = mount(
      <MachinesScreen
        rows={[local, remote]}
        stats={stats}
        places={{hosts: places, refresh: key => refreshed.push(key)}}
        repositories={{[remote.key]: '~/work'}}
        controls={controls(key => removed.push(key))}
        addMachine={() => undefined}
      />,
    );

    const localCard = card(screen.container, 'This machine');
    const remoteCard = card(screen.container, 'operator@host-a');
    expect(localCard.textContent).toContain('This computer');
    // An unset folder is not listed as a stand-in: only a folder somebody chose is said.
    expect(localCard.textContent).not.toContain('Host default');
    expect(localCard.textContent).toContain('11%');
    expect(localCard.textContent).toContain('local');
    expect(remoteCard.textContent).toContain('SSH to host-a as operator');
    expect(remoteCard.textContent).toContain('Repositories in ~/work');
    expect(remoteCard.textContent).toContain('73%');
    expect(remoteCard.textContent).toContain('gpu-cluster');
    expect(screen.controls('Remove This machine from this app')).toHaveLength(0);

    screen.press(screen.control('Refresh job places on operator@host-a'));
    screen.press(screen.control('Remove operator@host-a from this app'));
    expect(refreshed).toEqual([remote.key]);
    expect(removed).toEqual([remote.key]);
    screen.unmount();
  });

  it('says nothing of a user or port it cannot know instead of inventing one', () => {
    const screen = mount(
      <MachinesScreen
        rows={[
          {
            key: 'ssh:gpu-a',
            label: 'gpu-a',
            status: {kind: 'connecting', cause: null, staleForMs: null},
          },
        ]}
      />,
    );
    const machine = card(screen.container, 'gpu-a');
    expect(machine.textContent).toContain('SSH to gpu-a');
    expect(machine.textContent).not.toContain(' as ');
    expect(machine.textContent).not.toContain('Resolved by SSH');
    expect(machine.textContent).not.toContain('22');
    screen.unmount();
  });

  it('summarises machines and their places from the same joined facts', () => {
    const {screen} = open({rows: [local, refused], places: {local: readOf(response)}});
    const facts = [...screen.container.querySelectorAll('.vs-fact')].map(each => each.textContent);
    expect(facts).toEqual([
      '2 machines',
      '1 live',
      '5 job places',
      '1 place unreachable',
      'job places not read on 1 machine',
    ]);
    expect(screen.container.querySelectorAll('section.vs-card')).toHaveLength(2);
    screen.unmount();
  });

  it('keeps the dedicated Add machine action prominent in the empty state', () => {
    let opened = 0;
    const screen = mount(<MachinesScreen rows={[]} addMachine={() => (opened += 1)} />);
    expect(screen.text()).toContain('No machines connected');
    expect(screen.controls('Add machine or server')).toHaveLength(1);
    screen.press(screen.control('Add machine or server'));
    expect(opened).toBe(1);
    screen.unmount();
  });

  it('shows probe failures and unreachable reasons without calling unread data a status', () => {
    const reason = create(EvalPlaceSchema, {
      name: 'cluster-b',
      kind: EvalPlaceKind.SLURM,
      state: {
        case: 'unreadable',
        value: {reason: OffersUnreadableReason.UNREACHABLE, detail: 'connection timed out'},
      },
    });
    const {screen, draw} = open({
      places: {
        local: readOf(create(ListEvalPlacesResponseSchema, {places: [reason]})),
      },
    });
    const unreachable = [...screen.container.querySelectorAll<HTMLElement>('.vs-chip')].find(
      chip => chip.textContent === 'Unreachable',
    );
    expect(unreachable?.getAttribute('aria-description')).toBe('connection timed out');
    screen.render(
      draw({
        local: failedOf(
          readOf(create(ListEvalPlacesResponseSchema, {places: [reason]})),
          'host unreachable',
        ),
      }),
    );
    expect(screen.text()).not.toContain('Not read');
    const failure = [...screen.container.querySelectorAll<HTMLElement>('.vs-chip')].find(
      chip => chip.textContent === 'Probe failed',
    );
    expect(failure?.getAttribute('aria-description')).toBe('host unreachable');
    screen.unmount();
  });
});

describe('Refresh of a machine’s eval places', () => {
  it('asks only that host to probe again and offers no editing', () => {
    const {screen, refreshed} = open({places: {local: readOf(response)}});
    screen.press(screen.control('Refresh job places on This machine'));
    expect(refreshed).toEqual(['local']);
    expect(
      screen.container.querySelectorAll('table input, table select, table textarea'),
    ).toHaveLength(0);
    screen.unmount();
  });

  it('does not dim an automatic read, but dims a person-requested read until its answer replaces it', () => {
    const {screen, draw} = open({places: {local: readOf(response)}});
    screen.render(draw({local: readingOf(readOf(response))}));
    expect(dimmed(screen.container, 'This machine places')).toBe(0);

    screen.render(draw({local: readOf(response)}));
    screen.press(screen.control('Refresh job places on This machine'));
    expect(dimmed(screen.container, 'This machine places')).toBe(0);
    screen.render(draw({local: readingOf(readOf(response))}));
    expect(dimmed(screen.container, 'This machine places')).toBe(5 * 3);
    expect(screen.control('Refresh job places on This machine').getAttribute('aria-disabled')).toBe(
      'true',
    );

    const answer = create(ListEvalPlacesResponseSchema, {places: [notProbed]});
    screen.render(draw({local: readOf(answer)}));
    expect(dimmed(screen.container, 'This machine places')).toBe(0);
    expect(cells(screen.container, 'This machine places')).toEqual([
      ['cluster-c', 'Not probed', ''],
    ]);
    screen.unmount();
  });

  it('dims only the machine whose Refresh was pressed', () => {
    const rows = [local, refused];
    const both = {local: readOf(response), [refused.key]: readOf(response)};
    const {screen, draw} = open({rows, places: both});
    screen.press(screen.control('Refresh job places on This machine'));
    screen.render(draw({...both, local: readingOf(readOf(response))}));
    expect(dimmed(screen.container, 'This machine places')).toBe(5 * 3);
    expect(dimmed(screen.container, 'host-b places')).toBe(0);
    screen.unmount();
  });
});
