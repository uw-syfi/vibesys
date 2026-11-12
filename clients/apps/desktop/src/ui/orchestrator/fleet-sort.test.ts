import {describe, expect, it} from '@effect/vitest';
import {RunStatus} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';
import {FLEET_COLUMNS, type FleetRow, type FleetSort} from '../../state/index.js';
import {sortedRows} from './fleet-sort.js';

const row = fc
  .record({
    n: fc.nat(1000),
    live: fc.boolean(),
    host: fc.constantFrom('host-a', 'host-b', 'host-c'),
    cost: fc.option(fc.integer({min: 0, max: 5000}), {nil: null}),
    agents: fc.option(fc.nat(9), {nil: null}),
    percent: fc.option(fc.integer({min: 0, max: 100}), {nil: null}),
  })
  .map(
    ({n, live, host, cost, agents, percent}): FleetRow => ({
      source: 'managed',
      key: `${host}/r${n}`,
      host: {
        key: host,
        label: host,
        freshness: 'live',
        status: {kind: 'live', cause: null, staleForMs: null},
      },
      runId: `r${n}`,
      name: `task-${n % 7}`,
      card: null,
      repository: 'repo',
      setup: false,
      status: live ? RunStatus.RUNNING : RunStatus.ENDED,
      live,
      section: live ? 'live' : 'history',
      startedMs: n,
      endedMs: null,
      objectives:
        percent === null
          ? []
          : [{name: 'throughput', value: percent, unit: 'x', percentOfBound: percent}],
      costUsd: cost,
      budgetUsd: null,
      agentsRunning: agents,
      lastReportMs: null,
      reportUnavailable: false,
      deadlineMs: null,
      attention: [],
    }),
  );

/** Rows with distinct keys, as a list has: two rows never share a key. */
const uniqueRows = () => fc.uniqueArray(row, {maxLength: 30, selector: each => each.key});

const sort: fc.Arbitrary<FleetSort> = fc.record({
  column: fc.constantFrom(...FLEET_COLUMNS),
  direction: fc.constantFrom('asc' as const, 'desc' as const),
});

describe('sortedRows', () => {
  it('keeps the rows as they came when there is no sort', () => {
    fc.assert(
      fc.property(uniqueRows(), rows => {
        expect(sortedRows(rows, null)).toBe(rows);
      }),
    );
  });

  it('is a permutation of the rows, and sorting again changes nothing', () => {
    fc.assert(
      fc.property(uniqueRows(), sort, (rows, by) => {
        const once = sortedRows(rows, by);
        expect([...once].sort((a, b) => a.key.localeCompare(b.key))).toEqual(
          [...rows].sort((a, b) => a.key.localeCompare(b.key)),
        );
        expect(sortedRows(once, by)).toEqual(once);
      }),
    );
  });

  it('orders the numeric columns by their figure, with the rows that have none last either way', () => {
    const numeric: fc.Arbitrary<FleetSort> = fc.record({
      column: fc.constantFrom('cost' as const, 'agents' as const, 'best' as const),
      direction: fc.constantFrom('asc' as const, 'desc' as const),
    });
    const figure = (by: FleetSort, r: FleetRow): number | null =>
      by.column === 'cost'
        ? r.costUsd
        : by.column === 'agents'
          ? r.live
            ? r.agentsRunning
            : null
          : (r.objectives[0]?.percentOfBound ?? null);
    fc.assert(
      fc.property(uniqueRows(), numeric, (rows, by) => {
        const figures = sortedRows(rows, by).map(each => figure(by, each));
        const firstNone = figures.indexOf(null);
        const given = firstNone < 0 ? figures : figures.slice(0, firstNone);
        expect(figures.slice(given.length).every(each => each === null)).toBe(true);
        const sign = by.direction === 'asc' ? 1 : -1;
        for (let at = 1; at < given.length; at += 1) {
          expect(sign * ((given[at] ?? 0) - (given[at - 1] ?? 0))).toBeGreaterThanOrEqual(0);
        }
      }),
    );
  });

  it('puts the reverse order of a text column on the other end, and keeps ties in their order', () => {
    fc.assert(
      fc.property(
        uniqueRows(),
        fc.constantFrom('name' as const, 'host' as const),
        (rows, column) => {
          const asc = sortedRows(rows, {column, direction: 'asc'}).map(each =>
            each[column === 'name' ? 'name' : 'host'].valueOf(),
          );
          expect(asc).toHaveLength(rows.length);
          const hosts = sortedRows(rows, {column: 'host', direction: 'asc'}).map(
            each => each.host.label,
          );
          expect([...hosts]).toEqual([...hosts].sort());
          const tied = sortedRows(rows, {column: 'host', direction: 'desc'});
          for (const label of ['host-a', 'host-b', 'host-c']) {
            expect(tied.filter(each => each.host.label === label).map(each => each.key)).toEqual(
              rows.filter(each => each.host.label === label).map(each => each.key),
            );
          }
        },
      ),
    );
  });
});
