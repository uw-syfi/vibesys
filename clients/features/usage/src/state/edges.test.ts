import {describe, expect, test} from 'bun:test';
import {USAGE_QUARTER_MS, usageSummaryRefusal} from '@vibesys/hosts';
import fc from 'fast-check';
import {
  bucketLabel,
  bucketOf,
  type PeriodEdges,
  periodEdges,
  startOfLocalDay,
  USAGE_PERIODS,
  type UsagePeriod,
  zoneOffsetMs,
} from './edges.js';

/** Zones that break assumptions: a quarter-hour offset, a half-hour clock change, the date line. */
const ZONES = [
  'UTC',
  'America/Los_Angeles',
  'America/Sao_Paulo',
  'Asia/Kathmandu',
  'Asia/Kolkata',
  'Australia/Lord_Howe',
  'Pacific/Chatham',
  'Pacific/Kiritimati',
  'Pacific/Pago_Pago',
  'Europe/London',
  'Africa/Casablanca',
] as const;
const PERIODS = USAGE_PERIODS.map(each => each.value);
const COUNTS: Record<UsagePeriod, number> = {'24h': 24, '7d': 7, '30d': 30, '90d': 90};

const instants = fc.integer({
  min: Date.UTC(2023, 0, 1),
  max: Date.UTC(2027, 11, 31),
});

describe('periodEdges', () => {
  test('property: the edges are what a host accepts, and they cover now', () => {
    fc.assert(
      fc.property(
        fc.constantFrom(...PERIODS),
        instants,
        fc.constantFrom(...ZONES),
        (period, now, zone) => {
          const shown = periodEdges(period, now, zone);
          expect(usageSummaryRefusal(['/p'], shown.edges)).toBeNull();
          expect(shown.edges).toHaveLength(COUNTS[period] + 1);
          expect(shown.edges.every(edge => edge % USAGE_QUARTER_MS === 0)).toBe(true);
          const bucket = bucketOf(shown, now);
          expect(bucket).toBe(COUNTS[period] - 1);
        },
      ),
      {numRuns: 300, seed: 3544},
    );
  });

  test('property: days begin at local midnight and the first instant of a day is not in the day before', () => {
    fc.assert(
      fc.property(instants, fc.constantFrom(...ZONES), (now, zone) => {
        const shown = periodEdges('30d', now, zone);
        for (const edge of shown.edges) {
          expect(startOfLocalDay(edge, zone)).toBe(edge);
          expect(startOfLocalDay(edge - 1, zone)).toBeLessThan(edge);
        }
        // A day is 23, 24 or 25 hours, 24 and a half where a zone moves by half an hour.
        for (let at = 1; at < shown.edges.length; at += 1) {
          const length = (shown.edges[at] ?? 0) - (shown.edges[at - 1] ?? 0);
          expect(length).toBeGreaterThanOrEqual(22.5 * 3_600_000);
          expect(length).toBeLessThanOrEqual(25.5 * 3_600_000);
        }
      }),
      {numRuns: 200, seed: 3545},
    );
  });

  test('property: the past day is 24 hourly buckets that start on a local hour', () => {
    fc.assert(
      fc.property(instants, fc.constantFrom(...ZONES), (now, zone) => {
        const shown = periodEdges('24h', now, zone);
        for (const edge of shown.edges) {
          expect((edge + zoneOffsetMs(edge, zone)) % 3_600_000).toBe(0);
        }
        for (let at = 1; at < shown.edges.length; at += 1) {
          expect((shown.edges[at] ?? 0) - (shown.edges[at - 1] ?? 0)).toBe(3_600_000);
        }
      }),
      {numRuns: 200, seed: 3546},
    );
  });

  test('a day with a clock change is 23 or 25 hours, and a zone offset by a quarter hour still cuts whole quarters', () => {
    // Europe/London springs forward on 2026-03-29 and falls back on 2026-10-25.
    const spring = periodEdges('7d', Date.UTC(2026, 2, 30, 12), 'Europe/London');
    const lengths = spring.edges
      .slice(1)
      .map((edge, at) => (edge - (spring.edges[at] ?? 0)) / 3_600_000);
    expect(lengths).toContain(23);
    const autumn = periodEdges('7d', Date.UTC(2026, 9, 26, 12), 'Europe/London');
    expect(
      autumn.edges.slice(1).map((edge, at) => (edge - (autumn.edges[at] ?? 0)) / 3_600_000),
    ).toContain(25);
    const nepal = periodEdges('7d', Date.UTC(2026, 9, 26, 12), 'Asia/Kathmandu');
    expect(nepal.edges.every(edge => edge % USAGE_QUARTER_MS === 0)).toBe(true);
    expect(zoneOffsetMs(nepal.edges[0] ?? 0, 'Asia/Kathmandu')).toBe((5 * 60 + 45) * 60_000);
  });

  test('labels say the hour for a past day and the date for days', () => {
    const now = Date.UTC(2026, 9, 31, 19, 20);
    const hours: PeriodEdges = periodEdges('24h', now, 'UTC');
    expect(bucketLabel(hours, 23)).toBe('19:00');
    expect(bucketLabel(hours, 0)).toBe('20:00');
    const days = periodEdges('7d', now, 'America/Los_Angeles');
    expect(bucketLabel(days, 6)).toBe('Oct 31');
    expect(bucketLabel(days, 0)).toBe('Oct 25');
    expect(bucketLabel(days, 99)).toBe('');
  });

  test('property: a time belongs to the one bucket whose edges hold it', () => {
    fc.assert(
      fc.property(
        fc.constantFrom(...PERIODS),
        instants,
        fc.integer({min: -3, max: 100}),
        fc.nat(2_000_000_000),
        (period, now, step, jitter) => {
          const shown = periodEdges(period, now, 'Asia/Kolkata');
          const first = shown.edges[0] ?? 0;
          const at = first + step * 3_600_000 * 24 + jitter;
          const bucket = bucketOf(shown, at);
          const last = shown.edges[shown.edges.length - 1] ?? 0;
          if (at < first || at >= last) expect(bucket).toBeNull();
          else {
            expect(bucket).not.toBeNull();
            expect(shown.edges[bucket ?? 0] ?? Number.POSITIVE_INFINITY).toBeLessThanOrEqual(at);
            expect(shown.edges[(bucket ?? 0) + 1] ?? 0).toBeGreaterThan(at);
          }
        },
      ),
      {numRuns: 200, seed: 3547},
    );
  });
});
