/** Saved previous-writer runs retain input readings, ratios and chart references. */
import {expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {fromBinary} from '@bufbuild/protobuf';
import {SkeletonResponseSchema} from '@vibesys/proto';
import {DescriptorCache, ingestSkeleton} from '@vibesys/run-skeleton';
import {clockStart, heroView, ideasView, projectSkeleton} from './index.js';
import {firstChart} from './chart-fixtures.js';

const corpus = new URL('../../../../../proto/testdata/retained/', import.meta.url);
const cases = JSON.parse(readFileSync(new URL('manifest.json', corpus), 'utf8')) as Record<
  string,
  {
    input: number;
    best: number;
    ratio: number;
    measured: number;
    candidate: string;
    vs_baseline: number;
    chart_reference: number;
    measurement_seq: number;
  }
>;

for (const [version, facts] of Object.entries(cases)) {
  test(`previous writer ${version} preserves the input on every screen and historical seek`, () => {
    const response = fromBinary(
      SkeletonResponseSchema,
      readFileSync(new URL(`${version}/skeleton.response.pb`, corpus)),
    );
    const cache = new DescriptorCache();
    const decoded = cache.restore(response);
    const {skeleton} = ingestSkeleton(decoded);
    const through = skeleton.throughSeq ?? 0n;
    const model = projectSkeleton(skeleton, through);
    const hero = heroView(model);
    expect(hero.headlines[0]?.ratio).toBe(`${facts.ratio.toFixed(2)}×`);
    expect(skeleton.head?.performanceStated).toBe(true);
    expect(skeleton.head?.objective?.bestValue).toBe(facts.best);
    expect(hero.headlines[0]?.note).not.toBe('Unavailable');
    expect(firstChart(model, clockStart(model), model.latest)?.steps.at(-1)?.y).toBe(
      facts.best,
    );
    expect(
      ideasView(model, {passed: true, failed: true, running: true, query: ''}).rows.every(
        row => row.figure !== 'Performance facts unavailable',
      ),
    ).toBe(true);
    const gate = model.trace.spans.get(facts.candidate);
    const attrs = JSON.parse(gate?.label?.attrs_json ?? '{}');
    expect(model.input).toBe(facts.input);
    expect(attrs.input_metrics[0]?.value).toBe(facts.input);
    expect(attrs.vs_baseline).toBe(facts.vs_baseline);
    expect(firstChart(model, clockStart(model), model.latest).steps[0]?.y).toBe(
      facts.chart_reference,
    );
    for (let seq = 1n; seq <= through; seq += 1n) {
      const partial = projectSkeleton(skeleton, seq);
      expect(partial.input).toBe(seq >= BigInt(facts.measurement_seq) ? facts.input : null);
    }
  });
}
