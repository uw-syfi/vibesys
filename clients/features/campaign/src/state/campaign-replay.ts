/**
 * Turn a finished `CampaignRecord` into the ordered frame stream a live run
 * would have emitted. This is the single producer of frames for the prototype:
 * the dev SSE server calls it to replay a fixture "as if live", and the
 * round-trip test folds its output back to prove the projection is faithful
 * (`foldStateToRecord(foldFrames(framesFromRecord(r))) deep-equals r`).
 *
 * Frames are time-ordered so a client watching them arrive sees workstreams
 * start, measurements stream in, and workstreams finish in the order they
 * happened. The record has no usage data, so replay preserves that absence;
 * only an authoritative live producer may emit token frames.
 */

import type {CampaignFrame, CampaignHeader, FrameWorkstream} from './campaign-frames.js';
import type {CampaignRecord} from './campaign-record.js';

function headerOf(record: CampaignRecord): CampaignHeader {
  return {
    id: record.id,
    title: record.title,
    summary: record.summary,
    provenance: record.provenance,
    objective: record.objective,
    benchmarkVersions: record.benchmarkVersions,
    benchmarkVersionBoundary: record.benchmarkVersionBoundary,
    trajectories: record.trajectories,
  };
}

function inFlight(workstream: CampaignRecord['workstreams'][number]): FrameWorkstream {
  return {
    id: workstream.id,
    title: workstream.title,
    hypothesis: workstream.hypothesis,
    startedAt: workstream.startedAt,
    firstSequence: workstream.firstSequence,
    phase: 'implementing',
    active: true,
    finishedAt: null,
    lastSequence: null,
    outcome: null,
    outcomeSummary: '',
  };
}

function settled(workstream: CampaignRecord['workstreams'][number]): FrameWorkstream {
  return {
    id: workstream.id,
    title: workstream.title,
    hypothesis: workstream.hypothesis,
    startedAt: workstream.startedAt,
    firstSequence: workstream.firstSequence,
    // A finished record only records accepted/rejected outcomes, which are both
    // reached through the "evaluated" terminal phase; the outcome field carries
    // the verdict.
    phase: 'evaluated',
    active: false,
    finishedAt: workstream.finishedAt,
    lastSequence: workstream.lastSequence,
    outcome: workstream.outcome,
    outcomeSummary: workstream.outcomeSummary,
  };
}

interface TimedFrame {
  readonly at: number;
  readonly make: (seq: number) => CampaignFrame;
}

export function framesFromRecord(record: CampaignRecord): CampaignFrame[] {
  const makers: Array<(seq: number) => CampaignFrame> = [];
  makers.push(seq => ({kind: 'campaign-init', seq, header: headerOf(record)}));
  for (const agent of record.agents) makers.push(seq => ({kind: 'agent-upsert', seq, agent}));

  const timed: TimedFrame[] = [];
  for (const workstream of record.workstreams) {
    const started = Date.parse(workstream.startedAt);
    const finished = Date.parse(workstream.finishedAt);
    timed.push({
      at: started,
      make: seq => ({kind: 'workstream-upsert', seq, workstream: inFlight(workstream)}),
    });
    timed.push({
      at: finished,
      make: seq => ({kind: 'workstream-upsert', seq, workstream: settled(workstream)}),
    });
  }
  for (const measurement of record.measurements)
    timed.push({
      at: Date.parse(measurement.timestamp),
      make: seq => ({kind: 'measurement', seq, measurement}),
    });

  // Stable sort by time; ties keep insertion order, which already puts a
  // workstream's in-flight upsert before its settled one (finished >= started).
  timed.sort((left, right) => left.at - right.at);
  for (const frame of timed) makers.push(frame.make);

  makers.push(seq => ({kind: 'status', seq, status: 'completed'}));
  return makers.map((make, index) => make(index + 1));
}
