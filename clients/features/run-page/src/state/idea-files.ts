/**
 * The files each settled attempt changed, as the run wrote them on its workstream span
 * (`WorkstreamAttrs.files`), and the diff it attached under role `diff`. Every file names its
 * byte range in that diff; a file past the diff's size limit names none.
 *
 * The file list rides on the span, so it is known as soon as the attempt settles; the diff is a
 * blob the page reads only for the idea it opens (`followedDiffs`).
 */

import type {Skeleton} from '@vibesys/run-skeleton';
import type {CandidateAttempt} from '@vibesys/run-state/candidates';
import type {AttemptDiff, RunModel} from './run-model.js';
import {attrsOf} from './span-reading.js';

/** One changed file and its byte range in the attempt's diff; `length` 0: the diff left it out. */
export interface ChangedFile {
  readonly path: string;
  readonly offset: number;
  readonly length: number;
}

/** The diffs attached through `seq`, by workstream span, with the text of those already read. */
export function attemptDiffs(
  skeleton: Skeleton,
  seq: bigint,
  text: Readonly<Record<string, string>> = {},
): ReadonlyMap<string, AttemptDiff> {
  const diffs = new Map<string, AttemptDiff>();
  for (const record of skeleton.recordFacts) {
    if (record.seq > seq || record.body.case !== 'blob') continue;
    const {span, role, blob} = record.body.value;
    if (role !== 'diff' || blob === undefined) continue;
    diffs.set(span, {hash: blob.sha256, text: text[blob.sha256] ?? null});
  }
  return diffs;
}

/** A uint64 as the attrs' JSON reads it: a decimal string, or absent for zero. */
function count(value: unknown): number {
  const number = typeof value === 'string' || typeof value === 'number' ? Number(value) : 0;
  return Number.isSafeInteger(number) && number >= 0 ? number : 0;
}

/** The files `attempt` changed, as its span lists them; empty while it works or kept nothing. */
export function attemptFiles(model: RunModel, attempt: CandidateAttempt): readonly ChangedFile[] {
  const span = model.trace.spans.get(attempt.span);
  const files = span === undefined ? undefined : attrsOf(span)['files'];
  if (!Array.isArray(files)) return [];
  return files.flatMap(file => {
    if (typeof file !== 'object' || file === null) return [];
    const fields = file as Readonly<Record<string, unknown>>;
    const path = fields['path'];
    return typeof path === 'string' && path !== ''
      ? [{path, offset: count(fields['diff_offset']), length: count(fields['diff_length'])}]
      : [];
  });
}

/** The diff blobs of `attempts` the page should read: those of an opened idea. */
export function followedDiffs(model: RunModel, attempts: readonly CandidateAttempt[]): string[] {
  return attempts.flatMap(attempt => {
    const diff = model.diffs?.get(attempt.span);
    return diff === undefined ? [] : [diff.hash];
  });
}

/** How many UTF-8 bytes `char` (one code point) takes. */
function utf8Bytes(char: string): number {
  const code = char.codePointAt(0) ?? 0;
  return code < 0x80 ? 1 : code < 0x800 ? 2 : code < 0x10000 ? 3 : 4;
}

/**
 * Each file's part of `diff`, by its byte range, in one pass over the diff; null for a file the
 * diff left out, or whose range does not fall on whole characters of it.
 */
export function filePatches(diff: string, files: readonly ChangedFile[]): (string | null)[] {
  const wanted = new Set(
    files.flatMap(file => (file.length === 0 ? [] : [file.offset, file.offset + file.length])),
  );
  const indexAt = new Map<number, number>();
  let bytes = 0;
  let index = 0;
  for (const char of diff) {
    if (wanted.has(bytes)) indexAt.set(bytes, index);
    bytes += utf8Bytes(char);
    index += char.length;
  }
  if (wanted.has(bytes)) indexAt.set(bytes, index);
  return files.map(file => {
    if (file.length === 0) return null;
    const start = indexAt.get(file.offset);
    const end = indexAt.get(file.offset + file.length);
    return start === undefined || end === undefined ? null : diff.slice(start, end);
  });
}
