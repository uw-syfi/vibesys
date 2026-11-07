import {describe, expect, it} from '@effect/vitest';
import fc from 'fast-check';
import {renderToStaticMarkup} from 'react-dom/server';
import {applyChange, type Branch, emptyState, spawnTree} from '../../state/index.js';
import {generateScenario} from '../../testing/orchestrator/index.js';
import {TEST_CLOCK} from '../test-display-clock.js';
import {AgentsScreen, guidesOf} from './agents-screen.js';
import {formatSpan, formatUsd} from './time.js';

const {axisLabel, dayTime: formatDayTime, time: formatTime, offsetMs: zoneOffsetMs} = TEST_CLOCK;

const PROTOCOL = /UNSPECIFIED|AGENT_ROLE|TURN_|ITEM_STATUS|undefined|\[object|NaN/;

const branches = fc.array(fc.constantFrom<Branch>('more', 'last'), {minLength: 1, maxLength: 8});

describe('the lineage before an agent name', () => {
  it('has one cell per level, a line through the ancestors that continue, and a branch for the row', () => {
    fc.assert(
      fc.property(branches, lineage => {
        const guides = guidesOf(lineage);
        expect(guides).toHaveLength(lineage.length);
        guides.slice(0, -1).forEach((guide, level) => {
          expect(guide).toBe(lineage[level] === 'more' ? 'pipe' : 'blank');
        });
        expect(guides.at(-1)).toBe(lineage.at(-1) === 'more' ? 'tee' : 'elbow');
      }),
    );
  });

  it('has none for a row at the top', () => {
    expect(guidesOf([])).toEqual([]);
  });
});

function screenOf(seed: number, subagents: number, live: boolean, tree: boolean): string {
  const scenario = generateScenario({seed, subagents, live, tree});
  const state = scenario.changes.reduce(applyChange, emptyState);
  return renderToStaticMarkup(
    <AgentsScreen
      state={state}
      runId={scenario.runId}
      host="host-a"
      nowMs={scenario.endMs + 60_000}
      trail={[]}
      reading={null}
      open={() => undefined}
    />,
  );
}

describe('the agents screen', () => {
  it('says every agent it draws in words, for any run', () => {
    fc.assert(
      fc.property(
        fc.integer({min: 1, max: 60}),
        fc.integer({min: 0, max: 9}),
        fc.boolean(),
        (seed, subagents, live) => {
          expect(screenOf(seed, subagents, live, false)).not.toMatch(PROTOCOL);
        },
      ),
      {numRuns: 15},
    );
    fc.assert(
      fc.property(
        fc.integer({min: 1, max: 60}),
        fc.integer({min: 8, max: 40}),
        fc.boolean(),
        (seed, subagents, live) => {
          expect(screenOf(seed, subagents, live, true)).not.toMatch(PROTOCOL);
        },
      ),
      {numRuns: 15},
    );
  });

  it('draws the lead, its counts and its machine tag, and folds a big group of workers into one row', () => {
    const html = screenOf(5, 20, false, true);
    expect(html).toContain('lead');
    expect(html).toContain('host-a');
    expect(html).toMatch(/×12/);
    expect(html).toMatch(/\d+ total/);
  });

  it('draws as many rows as the tree has, up to what a window shows', () => {
    const scenario = generateScenario({seed: 3, subagents: 20, live: false, tree: true});
    const state = scenario.changes.reduce(applyChange, emptyState);
    const rows = spawnTree(state, scenario.runId, {nowMs: scenario.endMs, host: 'host-a'}).rows;
    const html = screenOf(3, 20, false, true);
    const drawn = (html.match(/role="option"/g) ?? []).length;
    expect(drawn).toBeGreaterThan(0);
    expect(drawn).toBeLessThanOrEqual(rows.length);
  });
});

describe('moments and spans', () => {
  it('tells a moment in Seattle time, whatever the zone of the machine', () => {
    // 17:00 UTC on 2026-11-01 is 9:00 AM in Seattle: daylight time ended at 09:00 UTC that day.
    const atMs = Date.UTC(2026, 10, 1, 17, 0, 0);
    expect(formatTime(atMs)).toBe('9:00 AM');
    expect(formatDayTime(atMs)).toBe('Sun 9:00 AM');
  });

  it('writes an axis in seconds for minutes, in the time for hours, and with the day for days', () => {
    const atMs = Date.UTC(2026, 10, 3, 18, 30, 5);
    expect(axisLabel(atMs, 60_000)).toBe('10:30:05 AM');
    expect(axisLabel(atMs, 3_600_000)).toBe('10:30 AM');
    expect(axisLabel(atMs, 3 * 86_400_000)).toBe('Tue 10:30 AM');
    expect(axisLabel(Date.UTC(2026, 10, 3, 8), 3 * 86_400_000)).toBe('Tue Nov 3');
  });

  it('labels a tick at Seattle midnight on a day axis with its date and no clock time', () => {
    fc.assert(
      fc.property(
        fc.integer({min: 18_000, max: 21_000}),
        fc.integer({min: 86_400_000, max: 40 * 86_400_000}),
        (day, span) => {
          // The midnight that starts Seattle's day number `day` since the epoch.
          const guess = day * 86_400_000;
          const midnight = guess - zoneOffsetMs(guess);
          const label = axisLabel(midnight, span);
          expect(label).not.toMatch(/\d:\d\d/);
          expect(label).toMatch(/^[A-Z][a-z]{2} [A-Z][a-z]{2} \d{1,2}$/);
        },
      ),
    );
  });

  it('knows how far Seattle is from UTC, in daylight time and out of it, so a day axis ticks at its midnights', () => {
    expect(zoneOffsetMs(Date.UTC(2026, 6, 1, 12))).toBe(-7 * 3_600_000);
    expect(zoneOffsetMs(Date.UTC(2026, 10, 3, 12))).toBe(-8 * 3_600_000);
    fc.assert(
      fc.property(fc.integer({min: 1_600_000_000_000, max: 1_900_000_000_000}), atMs => {
        const offset = zoneOffsetMs(atMs);
        expect([-7, -8]).toContain(offset / 3_600_000);
        expect(formatTime(atMs + 0)).toBe(
          new Date(atMs + offset).toLocaleTimeString('en-US', {
            timeZone: 'UTC',
            hour: 'numeric',
            minute: '2-digit',
          }),
        );
      }),
    );
  });

  it('writes spans in figures of equal width and never negative', () => {
    fc.assert(
      fc.property(fc.integer({min: -10_000, max: 10 ** 9}), ms => {
        expect(formatSpan(ms)).toMatch(/^\d+s$|^\d+m \d{2}s$|^\d+h \d{2}m$/);
      }),
    );
    expect(formatSpan(14_000)).toBe('14s');
    expect(formatSpan(185_000)).toBe('3m 05s');
    expect(formatSpan(7_800_000)).toBe('2h 10m');
  });

  it('writes a cost the host reported, and nothing for one it did not', () => {
    expect(formatUsd(1.1234)).toBe('$1.12');
    expect(formatUsd(0)).toBe('$0.00');
    expect(formatUsd(null)).toBe('');
  });
});
