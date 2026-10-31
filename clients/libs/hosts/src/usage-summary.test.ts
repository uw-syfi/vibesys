import {expect, test} from 'bun:test';
import fc from 'fast-check';
import {
  USAGE_QUARTER_MS,
  USAGE_SUMMARY_MAX_EDGES,
  USAGE_SUMMARY_MAX_PROJECTS,
  usageSummaryRefusal,
} from './usage-summary.js';

const edges = fc
  .tuple(
    fc.nat(1_000_000),
    fc.array(fc.integer({min: 1, max: 10_000}), {minLength: 1, maxLength: 130}),
  )
  .map(([start, steps]) => {
    const found = [start * USAGE_QUARTER_MS];
    for (const step of steps) found.push((found.at(-1) ?? 0) + step * USAGE_QUARTER_MS);
    return found;
  });

test('property: a question of 0 to 64 projects over 2 to 100 increasing quarter-hour edges is asked', () => {
  fc.assert(
    fc.property(
      fc.array(fc.string({minLength: 1}), {maxLength: USAGE_SUMMARY_MAX_PROJECTS}),
      edges.filter(each => each.length <= USAGE_SUMMARY_MAX_EDGES),
      (projects, found) => {
        expect(usageSummaryRefusal(projects, found)).toBeNull();
      },
    ),
  );
});

test('property: the same question is refused when it has too many projects or too many or too few edges', () => {
  fc.assert(
    fc.property(edges, found => {
      expect(
        usageSummaryRefusal(
          Array.from({length: USAGE_SUMMARY_MAX_PROJECTS + 1}, () => '/p'),
          found,
        ),
      ).toContain('projects');
      expect(usageSummaryRefusal(['/p'], found.slice(0, 1))).toContain('edges');
      expect(
        usageSummaryRefusal(
          ['/p'],
          Array.from({length: USAGE_SUMMARY_MAX_EDGES + 1}, (_, at) => at * USAGE_QUARTER_MS),
        ),
      ).toContain('edges');
    }),
  );
});

test('property: an edge off a quarter hour, or one that does not increase, is refused', () => {
  fc.assert(
    fc.property(
      edges.filter(each => each.length >= 3 && each.length <= 100),
      fc.nat(),
      (found, pick) => {
        const at = pick % found.length;
        const moved = found.map((edge, index) => (index === at ? edge + 1 : edge));
        expect(usageSummaryRefusal(['/p'], moved)).toContain('quarter hours');
        const flat = found.map((edge, index) =>
          index === at && index > 0 ? (found[index - 1] ?? 0) : edge,
        );
        if (at > 0) expect(usageSummaryRefusal(['/p'], flat)).toContain('increase');
      },
    ),
  );
});

test('property: an edge beyond the calendar a host holds is refused, however far', () => {
  fc.assert(
    fc.property(fc.integer({min: 1, max: 1_000_000}), steps => {
      const past = (253_402_300_799_000 / USAGE_QUARTER_MS + steps) * USAGE_QUARTER_MS;
      expect(
        usageSummaryRefusal(['/p'], [0, Math.floor(past / USAGE_QUARTER_MS) * USAGE_QUARTER_MS]),
      ).toContain('calendar');
    }),
  );
});
