/**
 * A transcript read by span: the entries the server tied to each span of the run trace, folded as
 * that span's own sequence.
 */
import {TranscriptBuffer, type TranscriptEntry} from './transcript-fold.js';

/**
 * The entries of `transcript` that belong to each of `spans`, by span id, each span's entries in
 * transcript order and folded again as if that span were the only one running: the run's
 * transcript folds a chunk into the entry before it only when that entry is the same turn's, so
 * chunks of turns that ran side by side stand apart there and join here. The same rule joins them,
 * so a span read out of the interleaved transcript is what folding that span's events alone gives.
 * A span with no entries is absent.
 *
 * Entries join the trace by `spanId` and by nothing else; one the server did not tie to a span is
 * in no span's transcript.
 */
export function transcriptBySpan(
  transcript: readonly TranscriptEntry[],
  spans: ReadonlySet<string>,
): ReadonlyMap<string, readonly TranscriptEntry[]> {
  const buffers = new Map<string, TranscriptBuffer>();
  for (const entry of transcript) {
    const span = entry.spanId;
    if (span === undefined || !spans.has(span)) continue;
    let buffer = buffers.get(span);
    if (buffer === undefined) {
      buffer = new TranscriptBuffer([]);
      buffers.set(span, buffer);
    }
    buffer.append(entry);
  }
  return new Map([...buffers].map(([span, buffer]) => [span, buffer.entries]));
}
