/**
 * What the Machines page is one table of: each machine with its connection status and live readings,
 * and under it the places it can run jobs at. This joins the three things the app knows of a machine
 * (the status row, the machine view, the eval places the host reported) into rows in the order the
 * table shows them, and counts them for the page's one-line summary, so the screen only draws.
 */
import type {EvalPlace, ListEvalPlacesResponse} from '@vibesys/orchestrator-proto';
import type {MachineView} from '../machine-stats.js';
import type {Remote} from '../new-run/remote.js';
import {type Reach, reachOf, unreadableDetail} from './eval-places.js';
import type {MachineRow} from './machine-rows.js';

/** One machine: its status, its readings and its places, each null when the window does not read it. */
export interface MachineEntry {
  readonly row: MachineRow;
  readonly stats: MachineView | null;
  readonly places: Remote<ListEvalPlacesResponse> | null;
}

/** The machine views and eval places of the hosts, by host key. */
export interface MachineSources {
  readonly stats?: readonly {readonly key: string; readonly view: MachineView}[];
  readonly places?: readonly {
    readonly key: string;
    readonly places: Remote<ListEvalPlacesResponse>;
  }[];
}

/** Each machine of `rows`, in their order, with the readings and places the sources hold for its key. */
export function machineEntries(
  rows: readonly MachineRow[],
  sources: MachineSources,
): MachineEntry[] {
  const views = new Map((sources.stats ?? []).map(each => [each.key, each.view]));
  const places = new Map((sources.places ?? []).map(each => [each.key, each.places]));
  return rows.map(row => ({
    row,
    stats: views.get(row.key) ?? null,
    places: places.get(row.key) ?? null,
  }));
}

/** The counts the page's summary line says. */
export interface MachinesFacts {
  readonly machines: number;
  /** Machines whose one status is live. */
  readonly live: number;
  /** Places the hosts reported, or null when the window does not read places at all. */
  readonly places: number | null;
  /** Places the host could not reach. */
  readonly unreachable: number;
  /** Machines whose places have not been read yet. */
  readonly unread: number;
}

const placesOf = (entry: MachineEntry): readonly EvalPlace[] => entry.places?.data?.places ?? [];

export function machinesFacts(entries: readonly MachineEntry[]): MachinesFacts {
  const reading = entries.filter(entry => entry.places !== null);
  const all = entries.flatMap(placesOf);
  return {
    machines: entries.length,
    live: entries.filter(entry => entry.row.status.kind === 'live').length,
    places: reading.length === 0 ? null : all.length,
    unreachable: all.filter(place => reachOf(place) === 'unreachable').length,
    unread: reading.filter(entry => entry.places?.data == null).length,
  };
}

/**
 * Whether the person asked for a machine's places to be read again, from the press until the read it
 * started ends: `asked` waits for that read to begin (the host's answer to the press is not in yet),
 * `reading` is that read. The read that opening the page starts is never an ask.
 */
export type PlaceAsk = 'none' | 'asked' | 'reading';

/** One machine's ask after a render that saw its places `reading` or not. */
const settledAsk = (ask: PlaceAsk, reading: boolean): PlaceAsk => {
  if (ask === 'asked' && reading) return 'reading';
  if (ask === 'reading' && !reading) return 'none';
  return ask;
};

/** `asks` after a render that saw each machine's places `reading` or not; the same map when nothing changed. */
export function settleAsks(
  asks: ReadonlyMap<string, PlaceAsk>,
  entries: readonly MachineEntry[],
): ReadonlyMap<string, PlaceAsk> {
  const next = new Map(asks);
  for (const {row, places} of entries) {
    const ask = asks.get(row.key) ?? 'none';
    const settled = places === null ? ask : settledAsk(ask, places.reading);
    if (settled === 'none') next.delete(row.key);
    else next.set(row.key, settled);
  }
  const same = next.size === asks.size && [...next].every(([key, ask]) => asks.get(key) === ask);
  return same ? asks : next;
}

/** `asks` with the press of the machine's Refresh in it. */
export const placesAsked = (
  asks: ReadonlyMap<string, PlaceAsk>,
  host: string,
): ReadonlyMap<string, PlaceAsk> => new Map(asks).set(host, 'asked');

/** What a machine's place list says when it is not a list of places. */
export type PlaceNote = 'waiting' | 'none' | 'not-read';

/** One row of the table, in its own key space (a host key, a place name and a note never collide). */
export type MachineTableRow =
  | {readonly kind: 'machine'; readonly key: string; readonly entry: MachineEntry}
  | {
      readonly kind: 'place';
      readonly key: string;
      readonly host: string;
      readonly place: EvalPlace;
      readonly reach: Reach;
      /** Why the host could not read it; empty when it did. */
      readonly detail: string;
      /** The person's Refresh is reading it again: it stays, quieter, until the answer replaces it. */
      readonly dim: boolean;
    }
  | {
      readonly kind: 'note';
      readonly key: string;
      readonly host: string;
      readonly note: PlaceNote;
      /** Why the last read failed, for `not-read`. */
      readonly failure: string | null;
    };

export const machineKey = (host: string): string => `machine:${host}`;

const noteRow = (host: string, note: PlaceNote, failure: string | null): MachineTableRow => ({
  kind: 'note',
  key: `note:${host}:${note}`,
  host,
  note,
  failure,
});

/** The lines under a machine that say what its places are not: waiting, none, or the failure of the last read. */
function noteRows(host: string, places: Remote<ListEvalPlacesResponse>): MachineTableRow[] {
  const {data, failure} = places;
  if (data === null) return [noteRow(host, failure === null ? 'waiting' : 'not-read', failure)];
  return [
    ...(data.places.length === 0 ? [noteRow(host, 'none', null)] : []),
    ...(failure === null ? [] : [noteRow(host, 'not-read', failure)]),
  ];
}

/** The rows under a machine: each of its places, then the lines that say what the places are not. */
function placeRows(entry: MachineEntry, dim: boolean): MachineTableRow[] {
  const {places, row} = entry;
  if (places === null) return [];
  const host = row.key;
  const occurrences = new Map<string, number>();
  return [
    ...placesOf(entry).map((place): MachineTableRow => {
      // A place name is not an identifier: valid host answers may repeat it. Its occurrence among
      // places with the same name is render identity only, derived from the host's stable order.
      const occurrence = occurrences.get(place.name) ?? 0;
      occurrences.set(place.name, occurrence + 1);
      return {
        kind: 'place',
        key: `place:${JSON.stringify([host, place.name, occurrence])}`,
        host,
        place,
        reach: reachOf(place),
        detail: unreadableDetail(place),
        dim,
      };
    }),
    ...noteRows(host, places),
  ];
}

/**
 * The rows of the table: each machine, then its places (or one line saying what they wait for), then
 * a line when the last read of them failed. A machine whose window does not read places has no lines.
 */
export const machineTableRows = (
  entries: readonly MachineEntry[],
  asks: ReadonlyMap<string, PlaceAsk>,
): MachineTableRow[] =>
  entries.flatMap((entry): MachineTableRow[] => [
    {kind: 'machine', key: machineKey(entry.row.key), entry},
    ...placeRows(entry, asks.get(entry.row.key) === 'reading'),
  ]);
