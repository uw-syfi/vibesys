/**
 * The live campaign telemetry contract: a closed union of typed delta frames.
 *
 * This is the proposed wire projection described in
 * `docs/design/live-campaign-streaming.md`. Each frame is a small delta; folding
 * the stream in order reconstructs a `CampaignRecord`. The frames are the single
 * boundary between an untrusted source (a dev replay server today, the
 * orchestrator's projection later) and the typed client state.
 *
 * `parseCampaignFrame` validates once, here. Everything downstream (the fold and
 * the view) consumes typed values and never re-validates.
 */
import {
  booleanAt,
  CampaignDecodeError,
  enumAt,
  exactKeys,
  fail,
  nullablePositiveIntegerAt,
  nullableTimestampAt,
  objectAt,
  parseAgent,
  parseCampaignHeader,
  parseMeasurement,
  positiveIntegerAt,
  stringAt,
  textAt,
  timestampAt,
} from './campaign-decoders.js';
import type {CampaignHeaderRecord, CampaignRecord, WorkstreamOutcome} from './campaign-record.js';

/** Workstream phases in the frontend-local replay preview contract. A production
 * campaign stream must use a backend-owned contract with generated client types. */
export type WorkstreamPhase =
  | 'pending'
  | 'implementing'
  | 'implemented'
  | 'reviewed'
  | 'evaluated'
  | 'failed'
  | 'parked'
  | 'cancelled';

const WORKSTREAM_PHASES: readonly WorkstreamPhase[] = [
  'pending',
  'implementing',
  'implemented',
  'reviewed',
  'evaluated',
  'failed',
  'parked',
  'cancelled',
];

/** The campaign-level data known at (or declared for) the start of a run. */
export type CampaignHeader = CampaignHeaderRecord;

export type FrameAgent = CampaignRecord['agents'][number];
export type FrameMeasurement = CampaignRecord['measurements'][number];

/**
 * A workstream delta. Unlike the finished-record shape, a live workstream may be
 * in flight: `active` is true, and `finishedAt`/`outcome`/`lastSequence` are not
 * known yet. The fold stores the phase; the view model projects an active
 * workstream's end to "now" so the existing dashboard renders it as active.
 */
export interface FrameWorkstream {
  readonly id: string;
  readonly title: string;
  readonly hypothesis: string;
  readonly startedAt: string;
  readonly firstSequence: number;
  readonly phase: WorkstreamPhase;
  readonly active: boolean;
  readonly finishedAt: string | null;
  readonly lastSequence: number | null;
  readonly outcome: WorkstreamOutcome | null;
  readonly outcomeSummary: string;
}

export type CampaignFrame =
  | {readonly kind: 'campaign-init'; readonly seq: number; readonly header: CampaignHeader}
  | {readonly kind: 'agent-upsert'; readonly seq: number; readonly agent: FrameAgent}
  | {readonly kind: 'workstream-upsert'; readonly seq: number; readonly workstream: FrameWorkstream}
  | {readonly kind: 'measurement'; readonly seq: number; readonly measurement: FrameMeasurement}
  | {
      readonly kind: 'tokens';
      readonly seq: number;
      readonly workstreamId: string;
      readonly tokens: number;
    }
  | {readonly kind: 'status'; readonly seq: number; readonly status: 'active' | 'completed'};

type CampaignFrameKind = CampaignFrame['kind'];

const FRAME_KINDS: readonly CampaignFrameKind[] = [
  'campaign-init',
  'agent-upsert',
  'workstream-upsert',
  'measurement',
  'tokens',
  'status',
];

class FrameError extends Error {}

function parseWorkstream(value: unknown, path: string): FrameWorkstream {
  const object = objectAt(value, path);
  exactKeys(object, path, [
    'id',
    'title',
    'hypothesis',
    'startedAt',
    'firstSequence',
    'phase',
    'active',
    'finishedAt',
    'lastSequence',
    'outcome',
    'outcomeSummary',
  ]);
  const startedAt = timestampAt(object.startedAt, `${path}.startedAt`);
  const firstSequence = positiveIntegerAt(object.firstSequence, `${path}.firstSequence`);
  const finishedAt = nullableTimestampAt(object.finishedAt, `${path}.finishedAt`);
  const lastSequence = nullablePositiveIntegerAt(object.lastSequence, `${path}.lastSequence`);
  if (finishedAt !== null && Date.parse(finishedAt) < Date.parse(startedAt))
    fail(`${path}.finishedAt`, 'must not precede startedAt');
  if (lastSequence !== null && lastSequence < firstSequence)
    fail(`${path}.lastSequence`, 'must not precede firstSequence');
  return {
    id: stringAt(object.id, `${path}.id`),
    title: stringAt(object.title, `${path}.title`),
    hypothesis: stringAt(object.hypothesis, `${path}.hypothesis`),
    startedAt,
    firstSequence,
    phase: enumAt(object.phase, `${path}.phase`, WORKSTREAM_PHASES),
    active: booleanAt(object.active, `${path}.active`),
    finishedAt,
    lastSequence,
    outcome:
      object.outcome === null
        ? null
        : enumAt(object.outcome, `${path}.outcome`, ['accepted', 'rejected'] as const),
    outcomeSummary: textAt(object.outcomeSummary, `${path}.outcomeSummary`),
  };
}

/**
 * Validate an unknown value as a campaign frame, rejecting an unknown kind,
 * an unexpected or missing key, or a wrong type, and naming the offending path.
 * Every nested field is decoded at this untrusted boundary. Relationships that
 * require earlier frames are deliberately left to the ordered stream fold.
 */
export function parseCampaignFrame(value: unknown): CampaignFrame {
  try {
    return decodeCampaignFrame(value);
  } catch (error) {
    if (error instanceof CampaignDecodeError) {
      throw new FrameError(`${error.path}: ${error.reason}`, {cause: error});
    }
    throw error;
  }
}

function decodeCampaignFrame(value: unknown): CampaignFrame {
  const object = objectAt(value, '$');
  const kind = enumAt(object.kind, '$.kind', FRAME_KINDS);
  const seq = positiveIntegerAt(object.seq, '$.seq');
  switch (kind) {
    case 'campaign-init': {
      exactKeys(object, '$', ['kind', 'seq', 'header']);
      return {kind, seq, header: parseCampaignHeader(object.header, '$.header')};
    }
    case 'agent-upsert': {
      exactKeys(object, '$', ['kind', 'seq', 'agent']);
      return {kind, seq, agent: parseAgent(object.agent, '$.agent')};
    }
    case 'workstream-upsert': {
      exactKeys(object, '$', ['kind', 'seq', 'workstream']);
      return {kind, seq, workstream: parseWorkstream(object.workstream, '$.workstream')};
    }
    case 'measurement': {
      exactKeys(object, '$', ['kind', 'seq', 'measurement']);
      return {
        kind,
        seq,
        measurement: parseMeasurement(object.measurement, '$.measurement'),
      };
    }
    case 'tokens': {
      exactKeys(object, '$', ['kind', 'seq', 'workstreamId', 'tokens']);
      return {
        kind,
        seq,
        workstreamId: stringAt(object.workstreamId, '$.workstreamId'),
        tokens: positiveIntegerAt(object.tokens, '$.tokens', true),
      };
    }
    case 'status': {
      exactKeys(object, '$', ['kind', 'seq', 'status']);
      return {
        kind,
        seq,
        status: enumAt(object.status, '$.status', ['active', 'completed'] as const),
      };
    }
  }
}

export function isTerminalPhase(phase: WorkstreamPhase): boolean {
  return phase === 'evaluated' || phase === 'failed' || phase === 'cancelled';
}
