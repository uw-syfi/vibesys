/**
 * The sessions of a run: the stretches of its run clock between one epoch header and the next. A
 * run that was stopped and resumed has one session per start; the run clock may or may not have run
 * on while it stood still (the writer's clock excludes the downtime, an older one does not), so a
 * session is read from the times its own records carry, never from the wall time of its header.
 *
 * Pure over a keyframe's version histories: the epoch headers (`startsEpoch`), the spans' start and
 * end times, and the metric points' clocks, each stamped with the sequence that recorded it.
 */
import type {StoreHistory} from './types.js';

/** The run-clock seconds a session's records span: its first time and its last. */
export interface RunSession {
  readonly from: number;
  readonly to: number;
}

/** A time the run recorded, and the sequence that recorded it. */
interface Stamp {
  readonly seq: bigint;
  readonly time: number;
}

/**
 * Each time a record states: a span's start at the sequence that first recorded it, and its end at
 * the first sequence that recorded it ended; a metric point's clock at its own sequence. A later
 * version of a span restates a time an earlier session recorded, so it adds no stamp.
 */
function stamps(history: StoreHistory, through: bigint): readonly Stamp[] {
  const found: Stamp[] = [];
  for (const spans of Object.values(history.spans)) {
    let ended = false;
    spans.versions.forEach((version, at) => {
      const span = version.span;
      if (version.seq > through || span === undefined) return;
      if (at === 0) found.push({seq: version.seq, time: span.startedAt});
      if (!ended && span.endedAt !== undefined) {
        ended = true;
        found.push({seq: version.seq, time: span.endedAt});
      }
    });
  }
  for (const metric of history.metrics) {
    for (const version of metric.points) {
      if (version.seq <= through && version.point) {
        found.push({seq: version.seq, time: version.point.clock});
      }
    }
  }
  return found;
}

/** The sequences at which a session after the first begins, in order. */
function resumes(history: StoreHistory, through: bigint): readonly bigint[] {
  return history.epochs
    .filter(version => version.startsEpoch && version.seq <= through)
    .map(version => version.seq)
    .sort((a, b) => (a < b ? -1 : a > b ? 1 : 0))
    .slice(1);
}

/**
 * The sessions of the run through sequence `through`, oldest first. A session none of the run's
 * records stamped a time in is left out, so every session returned has a first and a last time.
 */
export function sessionsOf(history: StoreHistory, through: bigint): readonly RunSession[] {
  const starts = resumes(history, through);
  const spans = new Map<number, {from: number; to: number}>();
  for (const {seq, time} of stamps(history, through)) {
    const session = starts.filter(start => start <= seq).length;
    const known = spans.get(session);
    if (known === undefined) spans.set(session, {from: time, to: time});
    else spans.set(session, {from: Math.min(known.from, time), to: Math.max(known.to, time)});
  }
  return [...spans.entries()].sort((a, b) => a[0] - b[0]).map(([, session]) => session);
}

/**
 * The latest run-clock time the run's records had stated by each of `seqs` (ascending), null before
 * any had: where each position of the run's history stands on its clock, for a time bar spaced by time.
 */
export function clockThrough(
  history: StoreHistory,
  seqs: readonly number[],
): readonly (number | null)[] {
  const through = seqs.at(-1);
  const ordered = [...stamps(history, through === undefined ? 0n : BigInt(through))].sort((a, b) =>
    a.seq < b.seq ? -1 : a.seq > b.seq ? 1 : 0,
  );
  const found: (number | null)[] = [];
  let reached: number | null = null;
  let next = 0;
  for (const seq of seqs) {
    for (let stamp = ordered[next]; stamp !== undefined && stamp.seq <= BigInt(seq); ) {
      reached = reached === null ? stamp.time : Math.max(reached, stamp.time);
      next += 1;
      stamp = ordered[next];
    }
    found.push(reached);
  }
  return found;
}
