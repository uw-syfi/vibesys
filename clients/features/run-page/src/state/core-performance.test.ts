/** Replay Python core/card outputs across the real protobuf boundary. */
import {expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {gunzipSync} from 'node:zlib';
import {fromBinary} from '@bufbuild/protobuf';
import {IdeaOutcome, KeyframeSchema, type RunHead, RunHeadSchema} from '@vibesys/proto';
import {seedSkeleton} from '@vibesys/run-skeleton';
import type {Candidate} from '@vibesys/run-state/candidates';
import {lifecycleState} from '@vibesys/run-state/skeleton';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {firstChart} from './chart-fixtures.js';
import {adoptedCandidate} from './end-words.js';
import {ideaTree} from './ideas-tree.js';
import {IDEAS_FILTER_DEFAULT} from './ideas-view.js';
import {clockStart, heroView, ideasView, projectSkeleton} from './index.js';
import type {RunModel} from './run-model.js';
import {ratio} from './span-reading.js';

const root = new URL('../../../../../proto/testdata/performance/', import.meta.url);
const cases: readonly {
  name: string;
  head: string;
  skeleton: string;
  card: {dashboard: {best: {value: number}}; adopted_ratio: number | null};
}[] = JSON.parse(gunzipSync(readFileSync(new URL('cases.json.gz', root))).toString());
for (const item of cases) {
  test(`core, card, hero and chart agree: ${item.name}`, () => {
    const head = fromBinary(RunHeadSchema, Buffer.from(item.head, 'base64'));
    const skeleton = seedSkeleton(fromBinary(KeyframeSchema, Buffer.from(item.skeleton, 'base64')));
    const model = projectSkeleton(skeleton, skeleton.throughSeq ?? 0n, '', {head});
    const facts = head.performance[0];
    expect(heroView(model).headlines[0]?.ratio).toBe(
      item.card.adopted_ratio === null ? null : `${item.card.adopted_ratio.toFixed(2)}×`,
    );
    expect(facts).toBeDefined();
    const kept = facts?.points.filter(point => point.newBest) ?? [];
    const chart = firstChart(model, clockStart(model), model.latest);
    // The line steps up exactly at the readings core calls a new best, to the best it states.
    const stepped = chart.steps.map(step => step.y).filter((y, at, ys) => y !== ys[at - 1]);
    expect(stepped.slice(chart.start === null ? 0 : 1)).toEqual(
      kept.flatMap(point => (point.point ? [point.point.value] : [])),
    );
    expect(chart.steps.at(-1)?.y).toBe(facts?.bestValue);
    expect(head.objective?.bestValue).toBe(item.card.dashboard.best.value);
    expect(facts?.bestValue).toBe(item.card.dashboard.best.value);
    expect(
      facts?.points
        .filter(point => point.outcome === IdeaOutcome.FAILED)
        .every(point => !point.newBest),
    ).toBe(true);
    expect<string | null>(lifecycleState(head.lifecycle).status).toBe(
      head.lifecycle?.state.case === 'ended'
        ? (head.lifecycle.state.value.presentation?.status ?? null)
        : null,
    );
    const catchingUp = projectSkeleton(skeleton, skeleton.throughSeq ?? 0n, '', {
      head,
      progress: {phase: 'history', through: 1n, committed: skeleton.throughSeq ?? 0n},
    });
    expect(heroView(catchingUp).headlines[0]?.ratio).toBeNull();
    for (const idea of catchingUp.candidates.candidates) {
      expect(
        ideasView(catchingUp, {passed: true, failed: true, running: true, query: ''}).rows.find(
          row => row.id === idea.id,
        )?.figure,
      ).toBe('Catching up');
    }
    const failed = {...catchingUp, loadFailure: 'Data loss'};
    expect(heroView(failed).headlines[0]?.note).toBe('Data loss');
    expect(heroView(failed).headlines[0]?.detail).not.toContain('Reading');
    for (const idea of failed.candidates.candidates)
      expect(
        ideasView(failed, {passed: true, failed: true, running: true, query: ''}).rows.find(
          row => row.id === idea.id,
        )?.figure,
      ).toBe('Data loss');
  });
}

type Case = (typeof cases)[number];

function modelOf(item: Case): {model: RunModel; head: RunHead} {
  const head = fromBinary(RunHeadSchema, Buffer.from(item.head, 'base64'));
  const skeleton = seedSkeleton(fromBinary(KeyframeSchema, Buffer.from(item.skeleton, 'base64')));
  return {model: projectSkeleton(skeleton, skeleton.throughSeq ?? 0n, '', {head}), head};
}

/** The ideas the head marks as new bests, in order; the head names an idea by its hypothesis. */
function statedNewBests(model: RunModel, head: RunHead): Candidate[] {
  const byStated = new Map(
    model.candidates.candidates.map(idea => [`hypothesis:${idea.hypothesis}`, idea]),
  );
  return (head.performance[0]?.points ?? []).flatMap(point => {
    const idea = point.newBest && point.idea !== undefined ? byStated.get(point.idea) : undefined;
    return idea === undefined ? [] : [idea];
  });
}

/**
 * The run as a client that infers would misread it: every idea core did not make a new best reads
 * as passed with a ratio beyond any best. The core facts, and so anything rendered from them,
 * are the same.
 */
function misleading(model: RunModel, head: RunHead): RunModel {
  const newBest = new Set(statedNewBests(model, head).map(idea => idea.id));
  const beyond = model.direction === 'min' ? 1e-6 : 1e6;
  return {
    ...model,
    candidates: {
      ...model.candidates,
      candidates: model.candidates.candidates.map(idea =>
        newBest.has(idea.id) || idea.measurement === null
          ? idea
          : {
              ...idea,
              outcome: 'passed',
              measurement: {...idea.measurement, vsBaseline: beyond, metricValue: beyond},
            },
      ),
    },
  };
}

/** The run with its ideas built on each other in a seeded shape: each on the input or an earlier one. */
function nested(model: RunModel, seed: number): RunModel {
  const random = new Seeded(seed);
  const ids = model.candidates.candidates.map(idea => idea.id);
  return {
    ...model,
    candidates: {
      ...model.candidates,
      candidates: model.candidates.candidates.map((idea, at) => ({
        ...idea,
        parent: at === 0 || random.int(4) === 0 ? null : (ids[random.int(at)] ?? null),
      })),
    },
  };
}

/** A second objective that core states the same series for, so its tile is an axis tile. */
function twoAxes(model: RunModel): RunModel {
  const name = model.series ? [...model.series.keys()][0] : undefined;
  const stated = name === undefined ? undefined : model.series?.get(name);
  if (name === undefined || stated === undefined) throw new Error('the case states no series');
  return {
    ...model,
    series: new Map([...(model.series ?? []), ['second_axis', stated]]),
    headline: undefined,
  };
}

/** The best below each idea that the head states: its latest new best strictly under it. */
function statedBestBelow(model: RunModel, head: RunHead): Map<string, number> {
  const parent = new Map(model.candidates.candidates.map(idea => [idea.id, idea.parent]));
  const below = new Map<string, number>();
  for (const idea of statedNewBests(model, head)) {
    const found = idea.measurement?.vsBaseline ?? null;
    if (found === null) continue;
    for (let at = idea.parent; at !== null; at = parent.get(at) ?? null) below.set(at, found);
  }
  return below;
}

let closedSummaries = 0;

const secondAxis = (model: RunModel) =>
  ({name: 'second_axis', direction: model.direction ?? 'max'}) as const;

/** The headline of the second axis, which core states the same series for. */
function axisTileOf(model: RunModel) {
  const axis = secondAxis(model);
  return heroView(twoAxes(model), [axis, axis]).headlines[0];
}

/** The chart of the second axis. */
function axisChartOf(model: RunModel) {
  const stated = twoAxes(model);
  return firstChart(stated, clockStart(stated), stated.latest, null, null, new Set(), [
    secondAxis(model),
  ]);
}

for (const item of cases) {
  test(`no surface infers a best from the ideas: ${item.name}`, () => {
    const {model, head} = modelOf(item);
    const misread = misleading(model, head);

    // The axis tile of a series reads the best core states, whatever the ideas passed with.
    expect(axisTileOf(misread)).toEqual(axisTileOf(model));
    expect(axisChartOf(model).steps.at(-1)?.y).toBe(head.performance[0]?.bestValue);

    // The Ideas tree's closed summary names core's latest new best below the idea.
    for (const seed of SEEDS.slice(0, 8)) {
      const stated = statedBestBelow(nested(model, seed), head);
      for (const view of [nested(model, seed), nested(misread, seed)]) {
        const shape = {filter: IDEAS_FILTER_DEFAULT, open: new Set<string>(), pinned: null};
        for (const entry of ideaTree(view, shape).rows) {
          if (entry.kind !== 'idea' || entry.summary === null) continue;
          closedSummaries += 1;
          const best = stated.get(entry.row.id);
          const shown = /best (\S+)/.exec(entry.summary)?.[1];
          expect(shown).toBe(best === undefined ? undefined : ratio(best));
        }
      }
    }

    // The ended banner names the adopted candidate from the search's adoption alone.
    const spans = [...model.trace.spans.values()];
    expect(adoptedCandidate(spans.filter(span => span.label?.kind !== 'adoption'))).toBeNull();
  });
}

test('the corpus has closed ideas for the tree summary to speak of', () => {
  expect(closedSummaries).toBeGreaterThan(0);
});
