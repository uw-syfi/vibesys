/** Replay Python core/card outputs across the real protobuf boundary. */
import {expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {gunzipSync} from 'node:zlib';
import {fromBinary} from '@bufbuild/protobuf';
import {IdeaOutcome, KeyframeSchema, RunHeadSchema} from '@vibesys/proto';
import {seedSkeleton} from '@vibesys/run-skeleton';
import {lifecycleState} from '@vibesys/run-state/skeleton';
import {
  clockStart,
  heroView,
  ideaFocusView,
  ideasView,
  projectSkeleton,
  stripChartView,
} from './index.js';

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
    expect(facts).toBeDefined();
    const kept = facts?.points.filter(point => point.newBest) ?? [];
    const chart = stripChartView(model, clockStart(model), model.latest);
    expect(
      chart?.points
        .filter(point => point.filled && point.tone === 'accent')
        .map(point => point.y) ?? [],
    ).toEqual(kept.flatMap(point => (point.point ? [point.point.value] : [])));
    expect(chart?.steps.at(-1)?.y).toBe(facts?.bestValue);
    expect(heroView(model).tiles[0]?.trend?.steps.at(-1)?.y).toBe(facts?.bestValue);
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
    expect(heroView(catchingUp).measured).toBeNull();
    expect(heroView(catchingUp).tiles[0]?.ratio).toBeNull();
    for (const idea of catchingUp.candidates.candidates) {
      expect(
        ideasView(catchingUp, {passed: true, failed: true, running: true, query: ''}).rows.find(
          row => row.id === idea.id,
        )?.figure,
      ).toBe('Catching up');
      expect(ideaFocusView(catchingUp, idea.id)?.ratio).toBeNull();
    }
    const failed = {...catchingUp, loadFailure: 'Data loss'};
    expect(heroView(failed).tiles[0]?.note).toBe('Data loss');
    expect(heroView(failed).tiles[0]?.detail).not.toContain('Reading');
    for (const idea of failed.candidates.candidates)
      expect(
        ideasView(failed, {passed: true, failed: true, running: true, query: ''}).rows.find(
          row => row.id === idea.id,
        )?.figure,
      ).toBe('Data loss');
  });
}
