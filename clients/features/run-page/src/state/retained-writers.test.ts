/** Saved previous-writer runs retain input readings, ratios and chart references. */
import {expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {fromBinary} from '@bufbuild/protobuf';
import {SkeletonResponseSchema} from '@vibesys/proto';
import {DescriptorCache, ingestSkeleton} from '@vibesys/run-skeleton';
import {heroView, projectSkeleton} from './index.js';

const corpus = new URL('../../../../../proto/testdata/retained/', import.meta.url);
const cases = JSON.parse(readFileSync(new URL('manifest.json', corpus), 'utf8')) as Record<
  string,
  {
    input: number;
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
    const gate = model.trace.spans.get(facts.candidate);
    const attrs = JSON.parse(gate?.label?.attrs_json ?? '{}');
    expect(model.input).toBe(facts.input);
    expect(attrs.input_metrics[0]?.value).toBe(facts.input);
    expect(attrs.vs_baseline).toBe(facts.vs_baseline);
    expect(heroView(model).tiles[0]?.trend?.reference).toBe(facts.chart_reference);
    for (let seq = 1n; seq <= through; seq += 1n) {
      const partial = projectSkeleton(skeleton, seq);
      expect(partial.input).toBe(seq >= BigInt(facts.measurement_seq) ? facts.input : null);
    }
  });
}
