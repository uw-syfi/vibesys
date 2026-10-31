/**
 * What a chart draws of each idea, over generated runs read from events and from a writer that keeps
 * more than one sample of an idea: one dot for each idea (every corner of the line is a dot), and,
 * with an idea open, a black ring on its dots and an accent ring only on the corners of the line its
 * lineage set.
 */
import {describe, expect, test} from 'bun:test';
import fc from 'fast-check';
import {candidateOf, generatedRuns, READERS, type Run} from './generated-runs.js';
import {objectiveCharts} from './objective-chart.js';
import type {RunModel} from './run-model.js';

const SETTINGS = {numRuns: 300} as const;

/** A generated run read as `read` says, with the open idea and the chain pointed at drawn from it. */
const moment = fc.record({run: generatedRuns, open: fc.nat(20), chain: fc.array(fc.nat(20))});

function chartOf(
  each: Run,
  read: (run: Run, candidates: ReturnType<typeof candidateOf>[]) => RunModel,
  open: number,
  chain: readonly number[],
) {
  const candidates = each.ideas.map((item, at) => candidateOf(item, at, each));
  const ids = candidates.map(item => item.id);
  const whole = read(each, candidates);
  const [chart] = objectiveCharts(whole, {
    start: 0,
    now: whole.latest,
    focus: ids[open % Math.max(1, ids.length)] ?? null,
    lit: new Set(chain.flatMap(at => ids[at % Math.max(1, ids.length)] ?? [])),
  });
  return chart;
}

for (const [reader, read] of READERS) {
  describe(`the readings of a chart, as read from ${reader}`, () => {
    test('are one dot for each idea, but every corner of the line', () => {
      fc.assert(
        fc.property(moment, ({run, open, chain}) => {
          const chart = chartOf(run, read, open, chain);
          const corners = new Set((chart?.steps ?? []).map(step => `${step.x}:${step.y}`));
          const readings = chart?.readings ?? [];
          for (const point of readings) {
            const same = readings.filter(other => other.id !== undefined && other.id === point.id);
            // Several dots of one idea are the corners it set, never a sample that set nothing.
            if (same.length > 1) expect(corners.has(`${point.x}:${point.y}`)).toBe(true);
          }
        }),
        SETTINGS,
      );
    });

    test('are ringed in the accent only where their idea’s chain set a corner of the line', () => {
      fc.assert(
        fc.property(moment, ({run, open, chain}) => {
          const chart = chartOf(run, read, open, chain);
          const corners = new Set((chart?.steps ?? []).map(step => `${step.x}:${step.y}`));
          for (const point of chart?.readings ?? []) {
            if (point.lit) expect(corners.has(`${point.x}:${point.y}`)).toBe(true);
          }
        }),
        SETTINGS,
      );
    });

    test('are ringed in black only for the idea that is open', () => {
      fc.assert(
        fc.property(moment, ({run, open, chain}) => {
          const chart = chartOf(run, read, open, chain);
          const ids = run.ideas.map((_, at) => `i${at}`);
          const focus = ids[open % Math.max(1, ids.length)] ?? null;
          for (const point of chart?.readings ?? []) {
            expect(point.selected).toBe(point.id === focus);
          }
        }),
        SETTINGS,
      );
    });
  });
}
