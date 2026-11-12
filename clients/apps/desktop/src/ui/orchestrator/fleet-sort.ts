import type {FleetColumn, FleetRow, FleetSort} from '../../state/index.js';
import {rowTitle} from './fleet-title.js';
import {genericRunWord} from './generic-run-words.js';
import {runStateWord} from './setup/index.js';

/** What a row is sorted by in a column: a word, a number, or null when it has nothing there. */
type SortKey = string | number | null;

/** The first objective's share of its bound; its best value when it has no bound; else nothing. */
function bestKey(row: FleetRow): SortKey {
  const [first] = row.objectives;
  if (first === undefined) return null;
  return first.percentOfBound ?? first.value;
}

const KEYS: Readonly<Record<FleetColumn, (row: FleetRow) => SortKey>> = {
  name: row => rowTitle(row),
  status: row =>
    row.source === 'generic'
      ? genericRunWord(row).label
      : runStateWord(row.setup, row.status).label,
  host: row => row.host.label,
  best: bestKey,
  agents: row => (row.live ? row.agentsRunning : null),
  cost: row => row.costUsd,
};

const compareKeys = (a: string | number, b: string | number): number =>
  typeof a === 'string' && typeof b === 'string'
    ? a.localeCompare(b, 'en', {numeric: true, sensitivity: 'base'})
    : Number(a) - Number(b);

/**
 * `rows` in the order of `sort`: by the column's key, ascending or descending, with the rows that
 * have nothing in the column last either way, and rows that tie in the order they came in. No sort
 * is `rows` as they are.
 */
export function sortedRows(rows: readonly FleetRow[], sort: FleetSort | null): readonly FleetRow[] {
  if (sort === null) return rows;
  const key = KEYS[sort.column];
  const sign = sort.direction === 'asc' ? 1 : -1;
  return rows
    .map((row, at) => ({row, at, key: key(row)}))
    .sort((a, b) => {
      if (a.key === null || b.key === null) {
        return a.key === b.key ? a.at - b.at : a.key === null ? 1 : -1;
      }
      return sign * compareKeys(a.key, b.key) || a.at - b.at;
    })
    .map(({row}) => row);
}
