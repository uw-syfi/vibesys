/**
 * The files a settled attempt changed, read from its workstream span the way the gateway path
 * reads it (`WorkstreamAttrs.files`), and each file's part of the diff the run attached.
 */
import {describe, expect, test} from 'bun:test';
import {storeRunFixture} from '@vibesys/run-fixtures';
import {foldSkeleton, seedSkeleton} from '@vibesys/run-skeleton';
import {SEEDS} from '@vibesys/store/testing';
import {section, settled} from './idea-files.test-helper.js';

const ENCODE = (text: string) => new TextEncoder().encode(text);

import {
  attemptFiles,
  changeCounts,
  diffBlock,
  filePatches,
  followedDiffs,
  projectSkeleton,
} from './index.js';

describe('the files a settled attempt changed', () => {
  test('every file is listed, and each opens its own part of the diff', () => {
    for (const seed of SEEDS) {
      const {read, idea, paths, kept, diff, hash} = settled(seed, ENCODE);
      const unread = read({});
      const attempt = unread.candidates.candidates.find(each => each.id === idea)?.attempts.at(-1);
      if (attempt === undefined) throw new Error(`seed ${seed}: no attempt`);
      const files = attemptFiles(unread, attempt);
      expect(files.map(file => file.path)).toEqual(paths);
      expect(followedDiffs(unread, [attempt])).toEqual([hash]);
      expect(unread.diffs?.get(idea)).toEqual({hash, text: null});
      const model = read({[hash]: diff});
      const text = model.diffs?.get(idea)?.text ?? '';
      expect(text).toBe(diff);
      const patches = filePatches(text, files);
      expect(patches).toEqual(
        paths.map((path, index) => (index < kept ? section(path, index) : null)),
      );
      for (const patch of patches) if (patch !== null) expect(changeCounts(patch)).toBe('+2 −1');
    }
  });

  test('an attempt that changed nothing lists nothing and follows no diff', () => {
    const {records} = storeRunFixture('live', 2, 7);
    const skeleton = foldSkeleton(seedSkeleton(), records);
    const model = projectSkeleton(skeleton, skeleton.throughSeq ?? 0n);
    for (const idea of model.candidates.candidates) {
      for (const attempt of idea.attempts) expect(attemptFiles(model, attempt)).toEqual([]);
      expect(followedDiffs(model, idea.attempts)).toEqual([]);
    }
  });
});

describe('a patch as a code block', () => {
  test('the fence is longer than any backtick run inside, so the patch is never cut short', () => {
    for (const seed of SEEDS) {
      const patch = `+${'`'.repeat(seed % 9)} tick ${seed}\n-${'`'.repeat((seed * 7) % 5)}\n`;
      const block = diffBlock(patch);
      const fence = block.match(/^`+/)?.[0] ?? '';
      const runs = (patch.match(/`+/g) ?? []).map(run => run.length);
      expect(fence.length).toBeGreaterThan(Math.max(2, ...runs));
      expect(block).toBe(`${fence}diff\n${patch.slice(0, -1)}\n${fence}`);
    }
  });
});
