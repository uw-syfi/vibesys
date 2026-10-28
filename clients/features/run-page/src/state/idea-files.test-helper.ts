/** A settled attempt with changed files and its diff attached, as the run's records say it. */
import {create, type MessageInitShape} from '@bufbuild/protobuf';
import {anyPack} from '@bufbuild/protobuf/wkt';
import {
  Change,
  RecordSchema,
  RuntimeType,
  SpanStatus,
  type Record as StoreRecord,
  WorkstreamAttrsSchema,
} from '@vibesys/proto';
import {storeRunFixture} from '@vibesys/run-fixtures';
import {foldSkeleton, seedSkeleton} from '@vibesys/run-skeleton';
import {projectSkeleton} from './skeleton-projection.js';

const NAMES = ['src/queue/ring.rs', 'bench é.rs', 'docs/naïve ✓.md', 'a', 'lib/`tick`.ts'];

/** By seed: the paths an attempt changed, and how many of them the capped diff still holds. */
function changedBy(seed: number) {
  const paths = Array.from(
    {length: 1 + (seed % 4)},
    (_, index) => `${seed}/${NAMES[(seed + index) % NAMES.length]}`,
  );
  return {paths, kept: seed % 5 === 0 ? paths.length - 1 : paths.length};
}

export function section(path: string, index: number): string {
  return `diff --git a/${path} b/${path}\n--- a/${path}\n+++ b/${path}\n@@ -1 +1,2 @@\n-old ${index}\n+new ${index}\n+more ✓\n`;
}

/**
 * The live fixture's last idea, settled with `paths` changed and the diff attached; `encode` is
 * the test's UTF-8 encoder, which names each file's byte range.
 */
export function settled(seed: number, encode: (text: string) => Uint8Array) {
  const count = 1 + (seed % 4);
  const {records} = storeRunFixture('live', count, seed);
  const idea = `idea-${count - 1}`;
  const {paths, kept} = changedBy(seed);
  const diff = paths.slice(0, kept).map(section).join('');
  let offset = 0;
  const files = paths.map((path, index) => {
    if (index >= kept) return {path};
    const length = encode(section(path, index)).length;
    const file = {path, diffOffset: BigInt(offset), diffLength: BigInt(length)};
    offset += length;
    return file;
  });
  const all: StoreRecord[] = [...records];
  const push = (body: NonNullable<MessageInitShape<typeof RecordSchema>['body']>) =>
    all.push(
      create(RecordSchema, {
        seq: BigInt(all.length + 1),
        epoch: 1,
        at: {seconds: 1700001000n + BigInt(all.length)},
        body,
      }),
    );
  push({
    case: 'blob',
    value: {
      span: idea,
      role: 'diff',
      blob: {sha256: `sha-${seed}`, size: 1n, mediaType: 'text/x-diff'},
    },
  });
  push({
    case: 'span',
    value: {
      change: Change.ENDED,
      span: {
        id: idea,
        runtimeType: RuntimeType.OPERATION,
        name: 'attempt',
        status: SpanStatus.DONE,
        startedAt: 0,
        endedAt: 9,
        label: {
          kind: 'workstream',
          title: 'attempt',
          attrs: anyPack(
            WorkstreamAttrsSchema,
            create(WorkstreamAttrsSchema, {parentRevision: 'a1', revision: 'b2', files}),
          ),
        },
      },
    },
  });
  const skeleton = foldSkeleton(seedSkeleton(), all);
  const read = (text: Record<string, string>) =>
    projectSkeleton(skeleton, skeleton.throughSeq ?? 0n, '', {content: {text}});
  return {read, idea, paths, kept, diff, hash: `sha-${seed}`};
}
