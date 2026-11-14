// Independent cross-check of the extractor's attribution invariant.
//
// Claimed self time must equal the sum over tasks of the union of that task's
// span segments: every on-CPU nanosecond covered by at least one span is
// claimed exactly once. Computed here from the toolkit directly, without
// reusing the extractor's accumulator, so a bug in the claiming order shows up
// as a mismatch rather than cancelling out.
//
// Spans are paired once over the whole trace, not per segment: an actor span's
// SpanEnter and SpanExit land in different rotated segments, so per-segment
// pairing would see neither side of it. This mirrors how the extractor defers
// attribution to `finalize`; the independence that matters is in the union
// arithmetic below, not in the shared `buildSpanData` primitive.
//
// Needs a real trace, so unlike the workload self-checks this cannot run on a
// machine that has never recorded one:
//
//   DIAL9_TOOLKIT=<dir> node check_invariants.js <trace-dir-or-file>
const M = require('./dial9_extract.js');

// Loaded inside main, not at module scope, so a missing toolkit reports the
// extractor's InputError message through the handler below instead of throwing
// a require stack past it.
let P;
let A;

async function main() {
  // Resolved through the extractor so both load the same toolkit; a
  // cross-check reading a different toolkit than the thing it checks is not a
  // cross-check.
  const toolkit = M.findToolkit(null);
  P = require(toolkit + '/trace_parser.js');
  A = require(toolkit + '/trace_analysis.js');

  const traceFile = process.argv[2];
  if (!traceFile) {
    throw new M.InputError(
      'pass the trace directory or file to check as the first argument'
    );
  }
  let perTaskUnion = 0;
  let globalSegments = [];
  let taskGroups = 0;
  const acc = M.createAccumulator();

  // Span inputs are merged across every chunk and paired once after the loop,
  // not per chunk. buildWorkerSpans and buildSpanData are toolkit primitives
  // both sides share; the merge mirrors the extractor's so the two see the same
  // spans. The independence this check rests on is the per-task union below,
  // computed by plain interval normalization, versus the extractor's
  // first-claim-wins IntervalSet. The workerId filter must match the
  // extractor's `eventType`/EVENT_TYPES test exactly: filtering on a `type`
  // field the events do not carry would admit QueueSample and WakeEvent rows
  // and build a different workerSpans than the thing under test.
  const { EVENT_TYPES } = P;
  const mergedWorkerSpans = {};
  const mergedCustom = [];
  const mergedTid = [];
  const mergedBip = [];

  for await (const trace of P.parseTrace(traceFile, {})) {
    const workerIds = [...new Set(
      trace.events
        .filter(e => e.eventType !== EVENT_TYPES.QueueSample && e.eventType !== EVENT_TYPES.WakeEvent)
        .map(e => e.workerId)
    )].sort((a, b) => a - b);

    const spans = A.buildWorkerSpans(trace.events, workerIds, trace.maxTs, trace.blockInPlaceGaps);
    for (const w of workerIds) {
      const ws = spans.workerSpans[w];
      if (!ws) continue;
      let mergedWs = mergedWorkerSpans[w];
      if (!mergedWs) { mergedWs = { polls: [] }; mergedWorkerSpans[w] = mergedWs; }
      for (const p of ws.polls) mergedWs.polls.push(p);
    }
    for (const e of trace.customEvents || []) mergedCustom.push(e);
    for (const b of trace.tidBindings || []) mergedTid.push(b);
    for (const g of trace.blockInPlaceGaps || []) mergedBip.push(g);

    M.accumulate(acc, trace, { parser: P, lib: A }, traceFile, -Infinity, Infinity);
  }

  // reconstructSpanSegments binary-searches each worker's polls and needs them
  // sorted ascending by start; concatenating per-chunk polls breaks that order.
  for (const w of Object.keys(mergedWorkerSpans)) {
    mergedWorkerSpans[w].polls.sort((a, b) => a.start - b.start);
  }

  const sd = A.buildSpanData(mergedCustom, mergedWorkerSpans, mergedTid, mergedBip);

  const byTask = new Map();
  for (const s of sd.allSpans) {
    const key = s.taskId == null ? '(unresolved)' : String(s.taskId);
    if (!byTask.has(key)) byTask.set(key, []);
    byTask.get(key).push(s);
  }
  taskGroups = byTask.size;

  for (const group of byTask.values()) {
    const segments = [];
    for (const s of group) for (const seg of (s.segments || [])) segments.push(seg);
    perTaskUnion += M.totalLength(M.normalize(segments));
    globalSegments = globalSegments.concat(segments);
  }

  const extracted = M.finalize(acc, -Infinity, Infinity);
  const claimed = extracted.components.reduce((n, c) => n + c.selfOnCpuNs, 0);
  const shareSum = extracted.components.reduce((n, c) => n + c.share, 0);

  console.log('task groups with spans', taskGroups);
  console.log('sum per-task unions   ', perTaskUnion);
  console.log('sum claimed self time ', claimed);
  console.log('match                 ', perTaskUnion === claimed ? 'YES' : 'NO');
  console.log('global union (too low)', M.totalLength(M.normalize(globalSegments)));
  console.log('pollOnCpuNs           ', extracted.totals.pollOnCpuNs);
  console.log('shareSum              ', shareSum.toFixed(6), shareSum <= 1 ? '(<= 1 ok)' : '(ABOVE 1)');
  console.log('unresolvedSpanCount   ', extracted.totals.unresolvedSpanCount);

  // The figures above are for reading; these are the assertions. Printing
  // `match NO` and exiting 0 would let a broken claiming order pass a check
  // that exists to catch exactly that.
  const failures = [];
  if (perTaskUnion !== claimed) {
    failures.push(
      `claimed self time ${claimed} ns does not equal the sum of per-task ` +
      `segment unions ${perTaskUnion} ns`
    );
  }
  if (shareSum > 1) {
    failures.push(`shares sum to ${shareSum.toFixed(6)}, above 1`);
  }
  if (taskGroups === 0) {
    failures.push('no task had any span, so nothing was actually checked');
  }
  if (failures.length > 0) {
    for (const failure of failures) console.error(`FAIL: ${failure}`);
    process.exitCode = 1;
    return;
  }
  console.log('ok: attribution invariant holds');
}

main().catch(e => {
  console.error(e instanceof M.InputError ? `error: ${e.message}` : e);
  process.exit(1);
});
