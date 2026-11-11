import {describe, expect, it} from '@effect/vitest';
import fc from 'fast-check';
import type {CostView} from '../cost.js';
import type {AgentsStrip} from './agents-strip.js';
import {runFacts} from './facts.js';
import {ideasReport} from './ideas.js';
import {reportsOf, searchOf, servedSeries} from './served-points.js';

const NO_AGENTS: AgentsStrip = {types: [], total: 0, running: 0};

const cost = (fields: {usd: number | null; turns: number; unpriced: number}): CostView =>
  ({
    total: {costUsd: fields.usd},
    turns: fields.turns,
    unpricedTurns: fields.unpriced,
  }) as unknown as CostView;

describe('the facts line of a report', () => {
  it('counts each measure job once, however many objectives it measured, and says when the host cut the view short', () => {
    fc.assert(
      fc.property(servedSeries, fc.boolean(), (series, truncated) => {
        const search = searchOf(series, truncated);
        const jobs = new Set(series.flatMap(each => (each.points ?? []).map(p => p.jobId)));
        const facts = runFacts(search, ideasReport(search), NO_AGENTS, undefined);
        expect(facts.measured.count).toBe(jobs.size);
        expect(facts.measured.atLeast).toBe(truncated);
        const newest = Math.max(...reportsOf(search).flatMap(o => o.points.map(p => p.atMs)));
        expect(facts.measured.latestMs).toBe(newest);
      }),
      {seed: 41},
    );
  });

  it('says nothing was measured, with no latest time, before the host has served any search view', () => {
    const facts = runFacts(undefined, undefined, NO_AGENTS, undefined);
    expect(facts.measured).toEqual({count: 0, atLeast: false, latestMs: null});
    expect(facts.ideas).toBeNull();
    expect(facts.spend).toBeUndefined();
  });

  it('counts the ideas of the run only, and takes the agents as the host counted them', () => {
    const search = searchOf([]);
    const facts = runFacts(
      search,
      ideasReport(search),
      {types: [], total: 28, running: 4},
      undefined,
    );
    expect(facts.ideas).toEqual({count: 0, atLeast: false});
    expect(facts.agents).toEqual({running: 4, total: 28});
  });

  it('calls a spend with unpriced turns a floor, and one with no priced turn no figure at all', () => {
    fc.assert(
      fc.property(
        fc.option(fc.double({min: 0, max: 1e5, noNaN: true}), {nil: null}),
        fc.integer({min: 0, max: 5000}),
        fc.integer({min: 0, max: 50}),
        (usd, turns, unpriced) => {
          const {spend} = runFacts(undefined, undefined, NO_AGENTS, cost({usd, turns, unpriced}));
          expect(spend?.usd).toBe(usd);
          expect(spend?.floor).toBe(usd !== null && unpriced > 0);
          expect(spend?.turns).toBe(turns);
          expect(spend?.unpricedTurns).toBe(unpriced);
        },
      ),
      {seed: 42},
    );
  });
});
