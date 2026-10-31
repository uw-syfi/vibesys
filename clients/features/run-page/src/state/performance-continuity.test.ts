/** Growing histories retain core facts across Tail, checkpoints and reordered watch hints. */
import {expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {gunzipSync} from 'node:zlib';
import {clone, create, fromBinary, toBinary} from '@bufbuild/protobuf';
import {
  KeyframeSchema,
  RecordSchema,
  RunHeadSchema,
  SkeletonResponseSchema,
  SpanHistorySchema,
  SpanVersionSchema,
  TailResponseSchema,
} from '@vibesys/proto';
import {ingestSkeleton, ingestTail, seedSkeleton} from '@vibesys/run-skeleton';
import {heroView, projectSkeleton} from './index.js';

const cases: {name: string; head: string; skeleton: string}[] = JSON.parse(
  gunzipSync(
    readFileSync(
      new URL('../../../../../proto/testdata/performance/cases.json.gz', import.meta.url),
    ),
  ).toString(),
);

for (const item of cases) {
  test(`performance never disappears on append or reconnect: ${item.name}`, () => {
    const frame = fromBinary(KeyframeSchema, Buffer.from(item.skeleton, 'base64'));
    const head = fromBinary(RunHeadSchema, Buffer.from(item.head, 'base64'));
    frame.head = head;
    let load = ingestSkeleton(
      create(SkeletonResponseSchema, {
        keyframe: toBinary(KeyframeSchema, frame),
        head,
        committedSeq: frame.throughSeq,
        nextSeq: frame.throughSeq + 1n,
        complete: true,
      }),
    );
    for (let step = 0; step < 4; step += 1) {
      const before = load.skeleton.head;
      const seq = (load.skeleton.throughSeq ?? 0n) + 1n;
      const nextHead = clone(RunHeadSchema, head);
      nextHead.committedSeq = seq;
      load = ingestTail(
        load,
        create(TailResponseSchema, {
          records: [toBinary(RecordSchema, create(RecordSchema, {seq, epoch: 1}))],
          throughSeq: seq,
          head: nextHead,
        }),
      );
      // The runs topic may be behind, ahead or missing while Tail is already committed.
      for (const hint of [undefined, before, {...nextHead, committedSeq: seq + 1n}, nextHead]) {
        const model = projectSkeleton(load.skeleton, seq, '', {head: hint});
        const performance = model.series?.get(head.performance[0]?.series ?? '');
        expect(performance).toBeDefined();
        expect(performance?.refusal).toBeUndefined();
        expect(heroView(model).tiles[0]?.note).not.toBe('Unavailable');
      }
      const checkpoint = create(KeyframeSchema, {...load.skeleton, throughSeq: seq});
      load = ingestSkeleton(
        create(SkeletonResponseSchema, {
          keyframe: toBinary(KeyframeSchema, checkpoint),
          head: nextHead,
          committedSeq: seq,
          nextSeq: seq + 1n,
          complete: true,
        }),
      );
      expect(load.skeleton.head?.performanceStated).toBe(true);
    }
  });

  test(`unmeasured final gates never hide measured idea ratios: ${item.name}`, () => {
    const frame = fromBinary(KeyframeSchema, Buffer.from(item.skeleton, 'base64'));
    const head = fromBinary(RunHeadSchema, Buffer.from(item.head, 'base64'));
    const measured = projectSkeleton(seedSkeleton(frame), frame.throughSeq, '', {head});
    assertCoreReadings(measured, head);
    for (let count = 1; count <= 3; count += 1) {
      const next = trailingGates(frame, measured.candidates.candidates, count);
      const currentHead = {...head, committedSeq: next.throughSeq};
      const model = projectSkeleton(seedSkeleton(next), next.throughSeq, '', {head: currentHead});
      for (const idea of measured.candidates.candidates) {
        if (idea.measurement?.metricValue === null || !idea.measurement) continue;
        const shown = model.candidates.candidates.find(candidate => candidate.id === idea.id);
        expect(shown?.measurement?.metricValue).toBe(idea.measurement.metricValue);
        expect(shown?.measurement?.vsBaseline).toBe(idea.measurement.vsBaseline);
        if (head.performance[0]?.inputValue) expect(shown?.measurement?.vsBaseline).not.toBeNull();
      }
    }
  });
}

function trailingGates(
  frame: import('@vibesys/proto').Keyframe,
  candidates: readonly import('@vibesys/run-state/candidates').Candidate[],
  count: number,
) {
  const next = clone(KeyframeSchema, frame);
  for (const candidate of candidates) {
    const gate = candidate.measurement?.span;
    const original = gate ? frame.spans[gate]?.versions.at(-1)?.span : undefined;
    if (!original) continue;
    for (let at = 0; at < count; at += 1) {
      const span = {
        ...original,
        id: `${original.id}-check-${at}`,
        startedAt: original.startedAt + 1000 + at,
        endedAt: (original.endedAt ?? original.startedAt) + 1000 + at,
      };
      const seq = ++next.throughSeq;
      next.spans[span.id] = create(SpanHistorySchema, {
        versions: [create(SpanVersionSchema, {seq, span})],
      });
    }
  }
  return next;
}

test('a Tail head from another committed prefix is rejected at ingestion', () => {
  const load = ingestSkeleton(
    create(SkeletonResponseSchema, {committedSeq: 0n, nextSeq: 1n, complete: true}),
  );
  expect(() =>
    ingestTail(
      load,
      create(TailResponseSchema, {
        records: [toBinary(RecordSchema, create(RecordSchema, {seq: 1n, epoch: 1}))],
        throughSeq: 1n,
        head: create(RunHeadSchema, {committedSeq: 2n, performanceStated: true}),
      }),
    ),
  ).toThrow('tail.head.committed_seq');
});

function assertCoreReadings(
  measured: ReturnType<typeof projectSkeleton>,
  head: import('@vibesys/proto').RunHead,
) {
  const facts = head.performance[0];
  for (const decision of facts?.ideas ?? []) {
    const point = facts?.points.find(
      point => point.measurementSeq === decision.measurementSeq,
    )?.point;
    if (!point) continue;
    const shown = measured.candidates.candidates.find(
      idea =>
        (idea.hypothesis === null ? `span:${idea.id}` : `hypothesis:${idea.hypothesis}`) ===
        decision.idea,
    );
    expect(shown?.measurement?.metricValue).toBe(point.value);
    if (facts?.inputValue)
      expect(shown?.measurement?.vsBaseline).toBe(point.value / facts.inputValue);
  }
}
