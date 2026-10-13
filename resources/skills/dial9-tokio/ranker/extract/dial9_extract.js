#!/usr/bin/env node
// Extract a deterministic, machine-readable attribution from dial9 traces.
//
// Usage: node dial9_extract.js <trace.bin|directory> [--start NS] [--end NS]
//                              [--toolkit DIR]
//
// Why this exists rather than `node analyze.js`: the toolkit's own entry point
// prints a human report and its structured result is not usable for ranking.
//   - `analyzeTraces()` takes no time window, so startup and ramp cannot be
//     excluded from a steady-state measurement.
//   - `pollDurationByLoc` keys on `spawn_loc`, a `#[track_caller]` location, so
//     every task spawned at one call site collapses into one bucket. An engine
//     that spawns all its operators from a single site gets one row.
//   - its `spanDurations` come from `buildSpanData(customEvents)` called with a
//     single argument, so span durations are wall-clock including awaits. On the
//     dial9 demo trace that reports p99 = 140 ms for `query_metric` inside a
//     4.5 s trace, which is await time, not CPU time.
//
// This reads the same toolkit libraries and reports per-span self on-CPU time,
// clipped to a window. Output is one JSON report written to --out, stable
// under re-runs.

'use strict';

const fs = require('fs');
const path = require('path');

const SCHEMA = 1;

/**
 * A rejected input, as opposed to a fault in this tool.
 *
 * The distinction is what the caller sees: an input error prints its message
 * alone, because a JS stack through `parseArgs` tells the operator nothing
 * about the flag they got wrong, while anything else keeps its stack because
 * there the frames are the only clue. `analyze_dial9.py` forwards stderr into
 * its own error, so suppressing the stack here is what keeps that message
 * readable.
 */
class InputError extends Error {}

// ── Toolkit resolution ──────────────────────────────────────────────────────
// The toolkit ships inside the dial9-toolkit skill. Its libraries are symlinks
// into the viewer's `ui/` directory in a source checkout, so a vendored copy
// must resolve symlinks. Located by content, never by directory name.
const TOOLKIT_FILES = ['trace_parser.js', 'trace_analysis.js', 'decode.js', 'diagnose_setup.js'];

// This extractor ships as first-party tooling inside the dial9-tokio skill
// bundle, one level up from the vendored toolkit, so it resolves its sibling
// toolkit by default and needs no --toolkit/DIAL9_TOOLKIT when run in place:
//   dial9-tokio/ranker/extract/  (here)  ->  dial9-tokio/skills/dial9-toolkit/scripts/
const BUNDLED_TOOLKIT = path.join(__dirname, '..', '..', 'skills', 'dial9-toolkit', 'scripts');

function findToolkit(explicit) {
  const candidates = [];
  if (explicit) candidates.push(path.resolve(explicit));
  if (process.env.DIAL9_TOOLKIT) candidates.push(path.resolve(process.env.DIAL9_TOOLKIT));
  candidates.push(BUNDLED_TOOLKIT);
  candidates.push(path.join(__dirname, 'toolkit'));
  const searched = [];
  for (const dir of candidates) {
    if (searched.includes(dir)) continue;
    searched.push(dir);
    if (TOOLKIT_FILES.every(f => fs.existsSync(path.join(dir, f)))) return dir;
  }
  throw new InputError(
    `cannot find the dial9 toolkit (${TOOLKIT_FILES.join(', ')}). Searched:\n  ` +
    searched.join('\n  ') +
    '\nPass --toolkit DIR or set DIAL9_TOOLKIT. Materialize it with ' +
    '`dial9 agents toolkit <dir>` from a crates.io install of dial9-viewer.'
  );
}

// ── Interval helpers ───────────────────────────────────────────────────────
// All timestamps are integer nanoseconds. Window clipping is a half-open
// intersection, so a segment exactly at the boundary contributes nothing and
// the same event is never counted in two adjacent windows.

function clippedLength(start, end, winStart, winEnd) {
  const a = Math.max(start, winStart);
  const b = Math.min(end, winEnd);
  return b > a ? b - a : 0;
}

function clippedTotal(segments, winStart, winEnd) {
  let total = 0;
  for (const seg of segments || []) total += clippedLength(seg.start, seg.end, winStart, winEnd);
  return total;
}

/** Merge overlapping intervals into a sorted, disjoint list. */
function normalize(intervals) {
  const sorted = [...intervals].sort((a, b) => (a.start - b.start) || (a.end - b.end));
  const out = [];
  for (const iv of sorted) {
    if (iv.end <= iv.start) continue;
    const last = out[out.length - 1];
    if (last && iv.start <= last.end) {
      if (iv.end > last.end) last.end = iv.end;
    } else {
      out.push({ start: iv.start, end: iv.end });
    }
  }
  return out;
}

function totalLength(intervals) {
  let total = 0;
  for (const iv of intervals) total += iv.end - iv.start;
  return total;
}

/**
 * A set of disjoint time intervals that reports and absorbs new ones.
 *
 * Used to attribute each on-CPU nanosecond to exactly one span. Spans are
 * offered innermost-first, so the most specific instrumentation claims the
 * time and an enclosing span keeps only what its children did not explain.
 */
class IntervalSet {
  constructor() {
    this.intervals = [];
  }

  /** Length of `candidates` not already claimed. Does not mutate. */
  lengthOutside(candidates) {
    const merged = normalize(candidates);
    let total = 0;
    let i = 0;
    for (const iv of merged) {
      let cursor = iv.start;
      while (i < this.intervals.length && this.intervals[i].end <= cursor) i++;
      let j = i;
      while (j < this.intervals.length && this.intervals[j].start < iv.end) {
        const claimed = this.intervals[j];
        if (claimed.start > cursor) total += Math.min(claimed.start, iv.end) - cursor;
        cursor = Math.max(cursor, claimed.end);
        if (cursor >= iv.end) break;
        j++;
      }
      if (cursor < iv.end) total += iv.end - cursor;
    }
    return total;
  }

  add(candidates) {
    this.intervals = normalize([...this.intervals, ...normalize(candidates)]);
  }
}

// ── Accumulator ────────────────────────────────────────────────────────────

function createAccumulator() {
  return {
    minTs: Infinity,
    maxTs: -Infinity,
    files: [],
    eventCount: 0,
    taskSpawnCount: 0,
    workerIds: new Set(),
    // spanName -> { selfOnCpuNs, wallNs, count, taskIds:Set }
    components: new Map(),
    // spawnLoc -> { onCpuNs, pollCount }
    spawnLocs: new Map(),
    totalPollOnCpuNs: 0,
    pollCount: 0,
    workerActiveNs: new Map(),
    workerParkNs: new Map(),
    workerPollCount: new Map(),
    schedDelays: [],
    queueMax: 0,
    queueSum: 0,
    queueCount: 0,
    longPolls: [],
    // Trace-level capability flags, ANDed across files: one uninstrumented
    // file makes the whole measurement untrustworthy, so the pessimistic
    // value has to win.
    hasFullTaskCoverage: true,
    hasTaskTracking: true,
    hasCpuTime: true,
    anyCustomEvents: false,
    // Spans whose owning task could not be resolved. Their self time is claimed
    // against a shared pool, so a nonzero count means attribution is approximate.
    unresolvedSpanCount: 0,
    cpuSampleCount: 0,
    offCpuSampleCount: 0,
    // Raw `{monotonicNs, realtimeNs}` pairs, in encounter order. The recorder
    // emits them periodically, so a long trace carries many and their implied
    // offsets drift apart as the two clocks are slewed independently.
    clockAnchors: [],
    // Span attribution is deferred to `finalize`, so these carry the raw inputs
    // across every segment first. A RisingWave actor span opens on an actor's
    // first poll and closes only at shutdown, so its SpanEnter and SpanExit land
    // in different rotated segments; pairing per segment (the obvious streaming
    // choice) would leave every such span unclosed and attribute nothing. The
    // cost is holding the poll index for the whole trace, which is bounded by
    // the trace's own size.
    customEvents: [],
    // workerId -> { polls: [...] }, the shape `buildSpanData` expects. Polls
    // from every segment for a given worker are concatenated here.
    mergedWorkerSpans: {},
    tidBindings: [],
    blockInPlaceGaps: [],
    // The toolkit analysis module, stashed so `finalize` can call `buildSpanData`
    // without the caller threading it through again. Set on first `accumulate`.
    lib: null,
  };
}

function bump(map, key, field, amount) {
  let rec = map.get(key);
  if (!rec) {
    rec = {};
    map.set(key, rec);
  }
  rec[field] = (rec[field] || 0) + amount;
}

function accumulate(acc, trace, analysis, sourceFile, winStart, winEnd) {
  const { EVENT_TYPES } = analysis.parser;
  const A = analysis.lib;
  acc.lib = A;

  acc.files.push(sourceFile);
  if (trace.minTs < acc.minTs) acc.minTs = trace.minTs;
  if (trace.maxTs > acc.maxTs) acc.maxTs = trace.maxTs;
  acc.eventCount += trace.events.length;
  acc.taskSpawnCount += trace.taskSpawnTimes ? trace.taskSpawnTimes.size : 0;
  acc.cpuSampleCount += trace.cpuSamples ? trace.cpuSamples.length : 0;
  for (const s of trace.cpuSamples || []) if (s.source === 1) acc.offCpuSampleCount++;
  if (trace.hasFullTaskCoverage === false) acc.hasFullTaskCoverage = false;
  if (trace.hasTaskTracking === false) acc.hasTaskTracking = false;
  if (trace.hasCpuTime === false) acc.hasCpuTime = false;
  for (const anchor of trace.clockSyncAnchors || []) acc.clockAnchors.push(anchor);

  const workerIds = [...new Set(
    trace.events
      .filter(e => e.eventType !== EVENT_TYPES.QueueSample && e.eventType !== EVENT_TYPES.WakeEvent)
      .map(e => e.workerId)
  )].sort((a, b) => a - b);
  for (const w of workerIds) acc.workerIds.add(w);

  const spans = A.buildWorkerSpans(trace.events, workerIds, trace.maxTs, trace.blockInPlaceGaps);
  // `cpuSamples` is absent when the 99 Hz stack sampler is off (the default this
  // example records under); on-CPU attribution comes from poll extents, not
  // samples, so an empty list here only means no hotspot stacks to attach.
  A.attachCpuSamples(trace.cpuSamples || [], spans.workerSpans);

  // Poll time is the denominator. Every poll is counted exactly once, clipped
  // to the window, whether or not a span covers it.
  for (const w of workerIds) {
    const ws = spans.workerSpans[w];
    if (!ws) continue;
    // Merge this segment's polls into the trace-wide index first, before the
    // window guard: span reconstruction in `finalize` intersects a span's
    // extent with its task's polls and only then clips to the window, so a poll
    // that straddles the window edge must survive here to contribute its
    // in-window part. Dropping out-of-window polls now would silently shorten
    // spans near the boundary.
    let mergedWs = acc.mergedWorkerSpans[w];
    if (!mergedWs) { mergedWs = { polls: [] }; acc.mergedWorkerSpans[w] = mergedWs; }
    for (const p of ws.polls) mergedWs.polls.push(p);
    for (const p of ws.polls) {
      const ns = clippedLength(p.start, p.end, winStart, winEnd);
      if (ns === 0) continue;
      acc.totalPollOnCpuNs += ns;
      acc.pollCount++;
      bump(acc.workerPollCount, w, 'n', 1);
      bump(acc.spawnLocs, p.spawnLoc || '(unknown)', 'onCpuNs', ns);
      bump(acc.spawnLocs, p.spawnLoc || '(unknown)', 'pollCount', 1);
      if (p.end - p.start > 1e6) {
        acc.longPolls.push({
          durNs: p.end - p.start,
          startNs: p.start,
          workerId: w,
          taskId: p.taskId || null,
          spawnLoc: p.spawnLoc || null,
          file: sourceFile,
        });
      }
    }
    for (const a of ws.actives || []) {
      bump(acc.workerActiveNs, w, 'ns', clippedLength(a.start, a.end, winStart, winEnd));
    }
    for (const p of ws.parks || []) {
      bump(acc.workerParkNs, w, 'ns', clippedLength(p.start, p.end, winStart, winEnd));
    }
  }

  // Span attribution is deferred to `finalize`: an actor span's enter and exit
  // routinely land in different rotated segments, so it can only be paired once
  // every segment has been read. Here we just carry the raw inputs forward.
  const customEvents = trace.customEvents || [];
  if (customEvents.length > 0) {
    acc.anyCustomEvents = true;
    for (const e of customEvents) acc.customEvents.push(e);
  }
  for (const b of trace.tidBindings || []) acc.tidBindings.push(b);
  for (const g of trace.blockInPlaceGaps || []) acc.blockInPlaceGaps.push(g);

  for (const sd of A.computeSchedulingDelays(spans.workerSpans, workerIds, spans.wakesByTask)) {
    acc.schedDelays.push(sd.delay);
  }
  for (const q of A.globalQueueSeries(trace, spans)) {
    if (q.global > acc.queueMax) acc.queueMax = q.global;
    acc.queueSum += q.global;
    acc.queueCount++;
  }
}

// ── Finalize ───────────────────────────────────────────────────────────────

function percentile(sorted, p) {
  if (sorted.length === 0) return null;
  const idx = Math.min(sorted.length - 1, Math.max(0, Math.ceil((p / 100) * sorted.length) - 1));
  return sorted[idx];
}

// Pair every span across the whole trace and claim on-CPU time to components.
// Split out of `accumulate` because pairing a SpanEnter in one segment with its
// SpanExit in a later one is only possible once all segments are in hand.
//
// `buildSpanData` with the trace-wide `workerSpans` intersects each raw span
// extent with its owning task's polls, so `segments` is real on-CPU time rather
// than wall time across awaits.
function attributeSpans(acc, winStart, winEnd) {
  if (!acc.anyCustomEvents || !acc.lib) return;
  const A = acc.lib;

  // `reconstructSpanSegments` binary-searches each worker's polls and relies on
  // them being sorted ascending by start; concatenating segments breaks that
  // order, so restore it once here.
  for (const w of Object.keys(acc.mergedWorkerSpans)) {
    acc.mergedWorkerSpans[w].polls.sort((a, b) => a.start - b.start);
  }

  const sd = A.buildSpanData(
    acc.customEvents, acc.mergedWorkerSpans, acc.tidBindings, acc.blockInPlaceGaps
  );

  // Self time is geometric, not parental. Nested instrumentation is normal (the
  // dial9 demo trace nests the `metrique` span `RecordMetric` inside the
  // `tracing` span `record_metric`), but spans produced by different instruments
  // carry `parentSpanId: null` for each other, so subtracting `childrenByParent`
  // leaves 63% of summed span time double counted on that trace and pushes total
  // share well above 1.
  //
  // Instead, each on-CPU nanosecond is claimed by exactly one span: offer spans
  // innermost-first (shortest extent) and give each only the time no tighter
  // span already took. For properly nested spans this is "innermost wins"; for
  // partial overlap it is first-claim-wins under a total order, so the result is
  // deterministic either way. Claiming is per task because one task's poll
  // segments are disjoint in time, while two tasks on different workers
  // legitimately overlap and their on-CPU time is additive.
  const spansByTask = new Map();
  for (const s of sd.allSpans) {
    if (clippedTotal(s.segments, winStart, winEnd) === 0) continue;
    const key = s.taskId == null ? '(unresolved)' : String(s.taskId);
    if (s.taskId == null) acc.unresolvedSpanCount++;
    const group = spansByTask.get(key);
    if (group) group.push(s); else spansByTask.set(key, [s]);
  }

  for (const group of spansByTask.values()) {
    group.sort((a, b) =>
      ((a.end - a.start) - (b.end - b.start)) ||
      (a.start - b.start) ||
      (String(a.spanId) < String(b.spanId) ? -1 : 1)
    );
    const claimed = new IntervalSet();
    for (const s of group) {
      const segments = (s.segments || [])
        .map(seg => ({
          start: Math.max(seg.start, winStart),
          end: Math.min(seg.end, winEnd),
        }))
        .filter(seg => seg.end > seg.start);
      const own = totalLength(normalize(segments));
      const self = claimed.lengthOutside(segments);
      claimed.add(segments);

      let rec = acc.components.get(s.spanName);
      if (!rec) {
        rec = { selfOnCpuNs: 0, onCpuNs: 0, wallNs: 0, count: 0, taskIds: new Set() };
        acc.components.set(s.spanName, rec);
      }
      rec.selfOnCpuNs += self;
      // Cumulative: this span's own on-CPU time, children included. Reported
      // for context only. Unlike selfOnCpuNs it may overlap other components.
      rec.onCpuNs += own;
      rec.wallNs += clippedLength(s.start, s.end, winStart, winEnd);
      rec.count++;
      if (s.taskId) rec.taskIds.add(s.taskId);
    }
  }
}

function finalize(acc, winStart, winEnd) {
  attributeSpans(acc, winStart, winEnd);

  // The denominator is in-window on-CPU poll time, not attributed time, so a
  // component's share stays comparable across runs whose instrumentation
  // coverage differs. Shares therefore sum to at most 1, not to exactly 1, and
  // the shortfall is `unattributedOnCpuNs`.
  const denominator = acc.totalPollOnCpuNs;
  const components = [...acc.components.entries()]
    .map(([name, rec]) => ({
      name,
      selfOnCpuNs: rec.selfOnCpuNs,
      share: denominator > 0 ? rec.selfOnCpuNs / denominator : 0,
      onCpuNs: rec.onCpuNs,
      wallNs: rec.wallNs,
      count: rec.count,
      taskCount: rec.taskIds.size,
    }))
    .filter(c => c.selfOnCpuNs > 0)
    .sort((a, b) => (b.selfOnCpuNs - a.selfOnCpuNs) || (a.name < b.name ? -1 : 1));

  const attributed = components.reduce((n, c) => n + c.selfOnCpuNs, 0);
  // Poll time no span covers. Reported rather than hidden: it is the honest
  // measure of how much of the runtime the instrumentation does not explain.
  const unattributed = Math.max(0, acc.totalPollOnCpuNs - attributed);

  const sortedDelays = [...acc.schedDelays].sort((a, b) => a - b);
  acc.longPolls.sort((a, b) => (b.durNs - a.durNs) || (a.startNs - b.startNs));

  const workers = [...acc.workerIds].sort((a, b) => a - b).map(w => {
    const active = (acc.workerActiveNs.get(w) || {}).ns || 0;
    const park = (acc.workerParkNs.get(w) || {}).ns || 0;
    const total = active + park;
    return {
      id: w,
      activeNs: active,
      parkNs: park,
      utilization: total > 0 ? active / total : 0,
      pollCount: (acc.workerPollCount.get(w) || {}).n || 0,
    };
  });

  const spawnLocs = [...acc.spawnLocs.entries()]
    .map(([loc, rec]) => ({ loc, onCpuNs: rec.onCpuNs || 0, pollCount: rec.pollCount || 0 }))
    .sort((a, b) => (b.onCpuNs - a.onCpuNs) || (a.loc < b.loc ? -1 : 1));

  return {
    schema: SCHEMA,
    trace: {
      files: [...acc.files].sort(),
      minTs: acc.minTs === Infinity ? null : acc.minTs,
      maxTs: acc.maxTs === -Infinity ? null : acc.maxTs,
      durationNs: acc.maxTs > acc.minTs ? acc.maxTs - acc.minTs : 0,
      eventCount: acc.eventCount,
      taskSpawnCount: acc.taskSpawnCount,
      cpuSampleCount: acc.cpuSampleCount,
      offCpuSampleCount: acc.offCpuSampleCount,
      workerCount: acc.workerIds.size,
      hasFullTaskCoverage: acc.hasFullTaskCoverage,
      hasTaskTracking: acc.hasTaskTracking,
      hasCpuTime: acc.hasCpuTime,
      hasSpans: acc.anyCustomEvents,
    },
    window: {
      startNs: winStart === -Infinity ? null : winStart,
      endNs: winEnd === Infinity ? null : winEnd,
    },
    totals: {
      pollOnCpuNs: acc.totalPollOnCpuNs,
      pollCount: acc.pollCount,
      attributedOnCpuNs: attributed,
      unattributedOnCpuNs: unattributed,
      unresolvedSpanCount: acc.unresolvedSpanCount,
    },
    components,
    spawnLocs: spawnLocs.slice(0, 50),
    workers,
    schedDelay: {
      count: sortedDelays.length,
      highCount: sortedDelays.filter(d => d > 1e6).length,
      p50Ns: percentile(sortedDelays, 50),
      p99Ns: percentile(sortedDelays, 99),
      maxNs: sortedDelays.length ? sortedDelays[sortedDelays.length - 1] : null,
    },
    queueDepth: {
      max: acc.queueMax,
      avg: acc.queueCount > 0 ? acc.queueSum / acc.queueCount : 0,
      samples: acc.queueCount,
    },
    longPolls: acc.longPolls.slice(0, 50),
  };
}

// ── Clock anchors ──────────────────────────────────────────────────────────

/**
 * Anchors sorted by realtime, each carrying its implied offset.
 *
 * Event timestamps are monotonic (nanoseconds since boot) while a workload
 * driver measures the wall clock, so a window has to be converted before it
 * can clip anything. `monotonicNs - realtimeNs` does that, but it is not a
 * single constant: the two clocks are slewed independently, so a long run's
 * anchors disagree. Keeping every anchor and picking the nearest bounds the
 * conversion error by the local offset instead of averaging it away.
 *
 * The toolkit hands both fields over as JS doubles (`num` in
 * `trace_parser.js`), so an epoch-scale `realtimeNs` arrives already quantized
 * to about 256 ns. That is the precision floor of any conversion here, and it
 * is orders of magnitude below the shortest window worth ranking.
 */
function clockAnchorTable(anchors) {
  return (anchors || [])
    .map(a => ({
      realtimeNs: Number(a.realtimeNs),
      monotonicNs: Number(a.monotonicNs),
      offsetNs: Number(a.monotonicNs) - Number(a.realtimeNs),
    }))
    .filter(a => Number.isFinite(a.realtimeNs) && Number.isFinite(a.monotonicNs))
    .sort((a, b) => (a.realtimeNs - b.realtimeNs) || (a.monotonicNs - b.monotonicNs));
}

/**
 * The reported `clock` block, or null when the trace carries no anchors.
 *
 * Null rather than a synthesized offset: a recorder built without clock sync
 * cannot be given one after the fact, and guessing would turn a loud failure
 * into a silently mis-clipped ranking. `offsetSpreadNs` is the honest upper
 * bound on how wrong a wall-clock window can be, so a consumer can see when
 * the two clocks drifted enough to matter.
 */
function summarizeClock(anchors) {
  const table = clockAnchorTable(anchors);
  if (table.length === 0) return null;
  const offsets = table.map(a => a.offsetNs).sort((a, b) => a - b);
  return {
    anchorCount: table.length,
    firstRealtimeNs: table[0].realtimeNs,
    lastRealtimeNs: table[table.length - 1].realtimeNs,
    firstMonotonicNs: table[0].monotonicNs,
    lastMonotonicNs: table[table.length - 1].monotonicNs,
    // An observed offset, not an interpolated one, so it is always a value the
    // machine actually reported.
    medianOffsetNs: percentile(offsets, 50),
    offsetSpreadNs: offsets[offsets.length - 1] - offsets[0],
  };
}

function unixToMonotonic(anchors, unixNs) {
  const table = clockAnchorTable(anchors);
  if (table.length === 0) {
    throw new InputError(
      'cannot convert a wall-clock window: the trace carries no clock sync anchors, ' +
      'so pass --start/--end in the trace monotonic timebase instead');
  }
  let best = table[0];
  for (const a of table) {
    if (Math.abs(a.realtimeNs - unixNs) < Math.abs(best.realtimeNs - unixNs)) best = a;
  }
  return unixNs + best.offsetNs;
}

// ── Entry point ────────────────────────────────────────────────────────────

// How the reported window was arrived at. Carried in the report because a
// consumer reading `window.startNs` cannot otherwise tell a converted
// wall-clock bound from one that was already monotonic.
const WINDOW_BASIS = Object.freeze({ FULL: 'full', MONOTONIC: 'monotonic', UNIX: 'unix' });

// Epoch nanoseconds are above 1e18 from 2001 onward, while the same instant in
// seconds, milliseconds or microseconds is at most ~1.8e15. Rejecting below the
// floor turns the one plausible unit mistake into an error that names the unit.
const MIN_UNIX_NS = 1e18;

const USAGE =
  'usage: dial9_extract.js <trace.bin|dir> --out PATH [--toolkit DIR]\n' +
  '       [--start NS] [--end NS] | [--start-unix-ns NS] [--end-unix-ns NS]';

function parseArgs(argv) {
  const out = {
    tracePath: null, outPath: null, startNs: -Infinity, endNs: Infinity,
    startUnixNs: null, endUnixNs: null, toolkit: null,
  };
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === '--start') out.startNs = Number(argv[++i]);
    else if (arg === '--end') out.endNs = Number(argv[++i]);
    else if (arg === '--start-unix-ns') out.startUnixNs = Number(argv[++i]);
    else if (arg === '--end-unix-ns') out.endUnixNs = Number(argv[++i]);
    else if (arg === '--toolkit') out.toolkit = argv[++i];
    else if (arg === '--out') out.outPath = argv[++i];
    else if (arg.startsWith('--')) throw new InputError(`unknown option: ${arg}`);
    else if (out.tracePath === null) out.tracePath = arg;
    else throw new InputError(`unexpected argument: ${arg}`);
  }
  if (out.tracePath === null) throw new InputError(USAGE);
  // The report goes to a file, never stdout: the toolkit's own `diagnoseSetup`
  // writes a human-readable diagnostic (including its `Fix:` blocks) to stdout
  // from inside the function, and that text is worth keeping in the run log
  // rather than silencing to keep a pipe clean.
  if (!out.outPath) throw new InputError(USAGE);
  if (!Number.isFinite(out.startNs) && out.startNs !== -Infinity) throw new InputError('--start must be a number');
  if (!Number.isFinite(out.endNs) && out.endNs !== Infinity) throw new InputError('--end must be a number');
  if (out.endNs <= out.startNs) throw new InputError('--end must be greater than --start');

  const hasMonotonic = out.startNs !== -Infinity || out.endNs !== Infinity;
  const hasUnix = out.startUnixNs !== null || out.endUnixNs !== null;
  // Mutually exclusive rather than merged: a window half in each timebase has
  // no meaning, and silently preferring one would hide the mistake.
  if (hasMonotonic && hasUnix) {
    throw new InputError('--start/--end and --start-unix-ns/--end-unix-ns are mutually exclusive');
  }
  for (const [flag, value] of [['--start-unix-ns', out.startUnixNs], ['--end-unix-ns', out.endUnixNs]]) {
    if (value === null) continue;
    if (!Number.isFinite(value)) throw new InputError(`${flag} must be a number`);
    if (value < MIN_UNIX_NS) {
      throw new InputError(`${flag} must be epoch nanoseconds (got ${value}, which looks like seconds, ms or us)`);
    }
  }
  if (out.startUnixNs !== null && out.endUnixNs !== null && out.endUnixNs <= out.startUnixNs) {
    throw new InputError('--end-unix-ns must be greater than --start-unix-ns');
  }
  return out;
}

/**
 * Reject a window that selects none of the trace.
 *
 * Without this the run still succeeds and reports zero components, which reads
 * as "nothing was instrumented" rather than "you clipped to the wrong slice".
 * Handing a monotonic timestamp to `--start-unix-ns`, or a wall-clock one to
 * `--start`, lands hundreds of thousands of seconds away from the trace and is
 * caught here.
 */
function assertWindowOverlapsTrace(report) {
  const { minTs, maxTs } = report.trace;
  if (minTs === null || maxTs === null) return;
  const start = report.window.startNs === null ? -Infinity : report.window.startNs;
  const end = report.window.endNs === null ? Infinity : report.window.endNs;
  if (start < maxTs && end > minTs) return;
  throw new InputError(
    `window [${start}, ${end}] does not overlap the trace [${minTs}, ${maxTs}] ` +
    `(basis ${report.window.basis}); the window is probably in the wrong timebase`);
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const toolkitDir = findToolkit(args.toolkit);
  const parser = require(path.join(toolkitDir, 'trace_parser.js'));
  const lib = require(path.join(toolkitDir, 'trace_analysis.js'));
  const analysis = { parser, lib };

  const acc = createAccumulator();
  const isDir = fs.statSync(args.tracePath).isDirectory();
  const label = isDir ? null : path.basename(args.tracePath);

  // Anchors are only known once a trace has been parsed, so converting a
  // wall-clock window needs a pre-pass before any event can be clipped. The
  // extra pass is paid only when a wall-clock window is actually requested,
  // and only collects anchors rather than running the full accumulator.
  let winStart = args.startNs;
  let winEnd = args.endNs;
  let basis = WINDOW_BASIS.FULL;
  if (args.startUnixNs !== null || args.endUnixNs !== null) {
    basis = WINDOW_BASIS.UNIX;
    const anchors = [];
    for await (const trace of parser.parseTrace(args.tracePath, {})) {
      for (const a of trace.clockSyncAnchors || []) anchors.push(a);
    }
    if (args.startUnixNs !== null) winStart = unixToMonotonic(anchors, args.startUnixNs);
    if (args.endUnixNs !== null) winEnd = unixToMonotonic(anchors, args.endUnixNs);
  } else if (winStart !== -Infinity || winEnd !== Infinity) {
    basis = WINDOW_BASIS.MONOTONIC;
  }

  for await (const trace of parser.parseTrace(args.tracePath, {})) {
    accumulate(acc, trace, analysis, label || trace.sourceFile || '(segment)', winStart, winEnd);
  }

  // Setup diagnostics come from the toolkit's own `diagnoseSetup` rather than
  // being reimplemented here, so the checks cannot drift from upstream's. It
  // reparses the trace, which is a second full pass, accepted because a build
  // flaw (no frame pointers, no tokio_unstable) silently invalidates the whole
  // ranking and is the one thing worth paying for twice.
  //
  // Severity is reported, not acted on: deciding which findings are fatal is
  // the consumer's policy, not the extractor's.
  const report = finalize(acc, winStart, winEnd);
  report.window.basis = basis;
  report.window.startUnixNs = args.startUnixNs;
  report.window.endUnixNs = args.endUnixNs;
  report.clock = summarizeClock(acc.clockAnchors);
  assertWindowOverlapsTrace(report);

  const { diagnoseSetup } = require(path.join(toolkitDir, 'diagnose_setup.js'));
  report.diagnostics = (await diagnoseSetup(args.tracePath)).map(f => ({
    severity: f.severity,
    check: f.check,
    message: f.message,
  }));

  fs.writeFileSync(args.outPath, JSON.stringify(report, null, 2) + '\n');
}

if (require.main === module) {
  main().catch(err => {
    const detail = err instanceof InputError
      ? `error: ${err.message}`
      : (err && err.stack ? err.stack : String(err));
    process.stderr.write(detail + '\n');
    process.exit(1);
  });
}

module.exports = {
  clippedLength, clippedTotal, normalize, totalLength, IntervalSet,
  createAccumulator, accumulate, finalize, parseArgs,
  clockAnchorTable, summarizeClock, unixToMonotonic, InputError,
  assertWindowOverlapsTrace, WINDOW_BASIS,
  // Exported so check_invariants.js resolves the toolkit by the same rule
  // rather than carrying a second copy of it. A cross-check that loaded a
  // different toolkit than the extractor would not be cross-checking anything.
  findToolkit,
};
