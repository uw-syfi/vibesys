import {create} from '@bufbuild/protobuf';
import {describe, expect, it} from '@effect/vitest';
import {
  EvalPlaceKind,
  EvalPlaceSchema,
  ListEvalPlacesResponseSchema,
  OffersUnreadableReason,
} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';
import {HOST_STATUS_KINDS} from '../host-status.js';
import {failedOf, NOT_READ, type Remote, readingOf, readOf} from '../new-run/remote.js';
import type {MachineRow} from './machine-rows.js';
import {
  type MachineEntry,
  type MachineTableRow,
  machineEntries,
  machinesFacts,
  machineTableRows,
  type PlaceAsk,
  placesAsked,
  settleAsks,
} from './machines-page.js';

const place = (name: string, reason?: OffersUnreadableReason) =>
  create(EvalPlaceSchema, {
    name,
    kind: EvalPlaceKind.SSH,
    ...(reason === undefined
      ? {state: {case: 'known', value: {}}}
      : {state: {case: 'unreadable', value: {reason}}}),
  });

const placesOf = (...names: string[]) =>
  readOf(create(ListEvalPlacesResponseSchema, {places: names.map(name => place(name))}));

const rowOf = (key: string, kind: MachineRow['status']['kind'] = 'live'): MachineRow => ({
  key,
  label: key,
  status: {kind, cause: null, staleForMs: null},
});

const entry = (
  key: string,
  places: Remote<never> | MachineEntry['places'] = null,
): MachineEntry => ({
  row: rowOf(key),
  stats: null,
  places,
});

const none = new Map<string, PlaceAsk>();
const kinds = (rows: readonly MachineTableRow[]): string[] => rows.map(row => row.kind);

describe('the table of machines and their places', () => {
  it('puts each machine first and its places under it, in the order the host gave them', () => {
    const rows = machineTableRows(
      [entry('host-a', placesOf('p1', 'p2')), entry('host-b', placesOf('q1'))],
      none,
    );
    expect(rows.map(row => row.key)).toEqual([
      'machine:host-a',
      'place:["host-a","p1",0]',
      'place:["host-a","p2",0]',
      'machine:host-b',
      'place:["host-b","q1",0]',
    ]);
  });

  it('keeps duplicate place names as distinct rows in the order the host gave them', () => {
    const rows = machineTableRows([entry('host-a', placesOf('same', 'same', 'other'))], none);
    const places = rows.filter(row => row.kind === 'place');
    expect(places.map(row => row.place.name)).toEqual(['same', 'same', 'other']);
    expect(new Set(places.map(row => row.key)).size).toBe(places.length);
  });

  it('has one machine row for each machine and a place row for each place, whatever they are', () => {
    const names = fc.array(fc.string({minLength: 1, maxLength: 6}), {maxLength: 6});
    const hosts = fc.uniqueArray(fc.string({minLength: 1, maxLength: 6}), {maxLength: 5});
    fc.assert(
      fc.property(hosts, fc.array(names, {maxLength: 5}), (keys, lists) => {
        const entries = keys.map((key, at) => entry(key, placesOf(...(lists[at] ?? []))));
        const rows = machineTableRows(entries, none);
        expect(rows.filter(row => row.kind === 'machine')).toHaveLength(keys.length);
        const total = entries.reduce(
          (sum, each) => sum + (each.places?.data?.places.length ?? 0),
          0,
        );
        expect(rows.filter(row => row.kind === 'place')).toHaveLength(total);
        // Every place sits somewhere after its own machine and before the next one.
        let host: string | null = null;
        for (const row of rows) {
          if (row.kind === 'machine') host = row.entry.row.key;
          else expect(row.host).toBe(host);
        }
        expect(new Set(rows.map(row => row.key)).size).toBe(rows.length);
      }),
    );
  });

  it('says what the places wait for, in one row, so a machine keeps a row under it', () => {
    expect(kinds(machineTableRows([entry('h', NOT_READ)], none))).toEqual(['machine', 'note']);
    const reading = machineTableRows([entry('h', readingOf(NOT_READ))], none)[1];
    expect(reading).toMatchObject({kind: 'note', note: 'waiting'});
    const empty = machineTableRows([entry('h', placesOf())], none)[1];
    expect(empty).toMatchObject({kind: 'note', note: 'none'});
    const failed = machineTableRows([entry('h', failedOf(NOT_READ, 'link lost'))], none)[1];
    expect(failed).toMatchObject({kind: 'note', note: 'not-read', failure: 'link lost'});
  });

  it('keeps the places a failed read leaves, with the failure after them', () => {
    const rows = machineTableRows([entry('h', failedOf(placesOf('p'), 'link lost'))], none);
    expect(kinds(rows)).toEqual(['machine', 'place', 'note']);
  });

  it('has no rows under a machine of a window that does not read places', () => {
    expect(kinds(machineTableRows([entry('h')], none))).toEqual(['machine']);
  });

  it('reads each place’s reach and the host’s reason, and dims only the machine asked about', () => {
    const answer = readOf(
      create(ListEvalPlacesResponseSchema, {
        places: [place('up'), place('down', OffersUnreadableReason.UNREACHABLE)],
      }),
    );
    const rows = machineTableRows(
      [entry('a', answer), entry('b', answer)],
      new Map<string, PlaceAsk>([['a', 'reading']]),
    );
    const places = rows.filter(row => row.kind === 'place');
    expect(places.map(row => (row.kind === 'place' ? [row.host, row.reach, row.dim] : []))).toEqual(
      [
        ['a', 'reachable', true],
        ['a', 'unreachable', true],
        ['b', 'reachable', false],
        ['b', 'unreachable', false],
      ],
    );
  });
});

describe('the summary of the page', () => {
  it('counts machines, the live ones, the places and the places that cannot be reached', () => {
    const answer = readOf(
      create(ListEvalPlacesResponseSchema, {
        places: [
          place('a'),
          place('b', OffersUnreadableReason.UNREACHABLE),
          place('c', OffersUnreadableReason.TIMED_OUT),
          place('d', OffersUnreadableReason.NOT_PROBEABLE),
        ],
      }),
    );
    const entries: MachineEntry[] = [
      {row: rowOf('a'), stats: null, places: answer},
      {row: rowOf('b', 'stale'), stats: null, places: NOT_READ},
      {row: rowOf('c', 'refused'), stats: null, places: failedOf(NOT_READ, 'x')},
    ];
    expect(machinesFacts(entries)).toEqual({
      machines: 3,
      live: 1,
      places: 4,
      unreachable: 2,
      unread: 2,
    });
  });

  it('counts the live machines exactly, for any statuses', () => {
    fc.assert(
      fc.property(fc.array(fc.constantFrom(...HOST_STATUS_KINDS), {maxLength: 8}), statuses => {
        const entries = statuses.map((kind, at) => ({
          row: rowOf(`h${at}`, kind),
          stats: null,
          places: null,
        }));
        const facts = machinesFacts(entries);
        expect(facts.machines).toBe(statuses.length);
        expect(facts.live).toBe(statuses.filter(kind => kind === 'live').length);
        expect(facts.places).toBeNull();
      }),
    );
  });

  it('counts no places at all when no machine reads them, instead of none', () => {
    expect(machinesFacts([entry('h')]).places).toBeNull();
    expect(machinesFacts([]).places).toBeNull();
  });
});

describe('joining what is known of a machine', () => {
  it('gives each row the readings and places of its own key, null for a key with none', () => {
    const entries = machineEntries([rowOf('a'), rowOf('b')], {
      places: [{key: 'b', places: placesOf('p')}],
    });
    expect(entries.map(each => each.places !== null)).toEqual([false, true]);
    expect(entries.map(each => each.stats)).toEqual([null, null]);
  });
});

describe('the person’s ask to read places again', () => {
  const answer = placesOf('p');
  const at = (reading: boolean): MachineEntry[] => [
    entry('h', reading ? readingOf(answer) : answer),
  ];

  it('is dimmed only from the read that follows the press until its answer, and then settles', () => {
    let asks: ReadonlyMap<string, PlaceAsk> = none;
    asks = placesAsked(asks, 'h');
    expect(asks.get('h')).toBe('asked');
    // The host’s answer to the press is not in yet: nothing is dimmed before the read begins.
    asks = settleAsks(asks, at(false));
    expect(asks.get('h')).toBe('asked');
    asks = settleAsks(asks, at(true));
    expect(asks.get('h')).toBe('reading');
    asks = settleAsks(asks, at(false));
    expect(asks.has('h')).toBe(false);
  });

  it('is never made by a read the person did not ask for', () => {
    expect(settleAsks(none, at(true))).toBe(none);
  });

  it('is the same map when nothing changed, so a render settles once', () => {
    const asks = placesAsked(none, 'h');
    expect(settleAsks(asks, at(false))).toBe(asks);
  });
});
