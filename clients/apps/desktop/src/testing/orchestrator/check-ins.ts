/**
 * Check-ins as a host serves them, made through the generated schemas, and generators of the
 * sequences a viewer's presses make. The figures are the test's: nothing here works out a window,
 * because nothing in the app does. A host's own rules (a viewer's windows follow one another, a
 * failed one does not move the next one's start) are what the generators keep, so a test can state
 * what the app does with them.
 */
import {create} from '@bufbuild/protobuf';
import {timestampFromMs} from '@bufbuild/protobuf/wkt';
import {
  AgentRole,
  type BestValue,
  BestValueSchema,
  CatchUpRequestSchema,
  type CheckIn,
  CheckInSchema,
  CheckInState,
  Delivery,
  type Message,
  MessageSchema,
  ObjectiveDirection,
  ObjectiveMoveSchema,
  ReportKind,
  ReportSchema,
  RoleSpendSchema,
  UsageSchema,
  ViewerSchema,
  type WindowFacts,
  WindowFactsSchema,
  WindowSpendSchema,
} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';

/** One viewer, an install of the app and the name it shows. */
const viewerOf = (id: string, label = `${id}'s laptop`) => create(ViewerSchema, {id, label});

/** What a role spent in a window, with the cost left out when `costUsd` is absent (never zero). */
export function roleSpend(
  role: AgentRole,
  turns: number,
  unpricedTurns: number,
  costUsd: number | undefined,
) {
  return create(RoleSpendSchema, {
    role,
    turns,
    unpricedTurns,
    usage: create(UsageSchema, {
      inputTokens: BigInt(turns * 1000),
      outputTokens: BigInt(turns * 100),
      ...(costUsd === undefined ? {} : {costUsd}),
    }),
  });
}

/** The best value of an objective up to `atMs`, from the job and commit that made it. */
export function bestValue(value: number, atMs: number, job: string): BestValue {
  return create(BestValueSchema, {
    value,
    jobId: job,
    commit: `${job}abcdef`.slice(0, 12),
    at: timestampFromMs(atMs),
  });
}

/** The figures a window freezes. Every field is optional so a test says only what it reads. */
export interface FactsFields {
  readonly roles?: ReturnType<typeof roleSpend>[];
  readonly turns?: number;
  readonly unpricedTurns?: number;
  /** The whole window's cost; absent when no turn was priced. */
  readonly costUsd?: number | undefined;
  readonly agentsStarted?: number;
  readonly agentsEnded?: number;
  readonly jobsMeasured?: number;
  readonly objectives?: readonly {
    readonly metric: string;
    readonly direction: ObjectiveDirection;
    readonly start?: BestValue | undefined;
    readonly end: BestValue;
  }[];
  readonly plansCreated?: readonly number[];
  readonly plansChanged?: readonly number[];
  readonly reportMessageIds?: readonly string[];
}

export function windowFactsOf(fields: FactsFields = {}): WindowFacts {
  const roles = fields.roles ?? [];
  const turns = fields.turns ?? roles.reduce((sum, role) => sum + role.turns, 0);
  return create(WindowFactsSchema, {
    spend: create(WindowSpendSchema, {
      turns,
      unpricedTurns: fields.unpricedTurns ?? roles.reduce((sum, r) => sum + r.unpricedTurns, 0),
      ...(turns === 0
        ? {}
        : {
            usage: create(UsageSchema, {
              inputTokens: BigInt(turns * 1000),
              outputTokens: BigInt(turns * 100),
              ...(fields.costUsd === undefined ? {} : {costUsd: fields.costUsd}),
            }),
          }),
      roles,
    }),
    agentsStarted: fields.agentsStarted ?? 0,
    agentsEnded: fields.agentsEnded ?? 0,
    jobsMeasured: fields.jobsMeasured ?? 0,
    objectives: (fields.objectives ?? []).map(objective =>
      create(ObjectiveMoveSchema, {
        metric: objective.metric,
        direction: objective.direction,
        ...(objective.start === undefined ? {} : {start: objective.start}),
        end: objective.end,
      }),
    ),
    plansCreated: [...(fields.plansCreated ?? [])],
    plansChanged: [...(fields.plansChanged ?? [])],
    reportMessageIds: [...(fields.reportMessageIds ?? [])],
  });
}

export interface CheckInFields {
  readonly id: string;
  readonly runId: string;
  readonly viewer: string;
  readonly sinceMs: number;
  readonly atMs: number;
  readonly state: CheckInState;
  readonly facts?: WindowFacts | undefined;
  readonly failure?: string;
  readonly ask?: string;
  readonly report?: string;
}

/** A check-in as the host stores it: facts only when ready, a reason only when failed. */
export function checkInOf(fields: CheckInFields): CheckIn {
  return create(CheckInSchema, {
    id: fields.id,
    runId: fields.runId,
    viewer: viewerOf(fields.viewer),
    since: timestampFromMs(fields.sinceMs),
    at: timestampFromMs(fields.atMs),
    state: fields.state,
    ...(fields.state === CheckInState.READY ? {facts: fields.facts ?? windowFactsOf()} : {}),
    failure:
      fields.state === CheckInState.FAILED ? (fields.failure ?? 'the job log is not read') : '',
    askMessageId: fields.ask ?? '',
    reportMessageId: fields.report ?? '',
  });
}

/** The message that asks a lead for a report, in whatever state of delivery. */
export function askMessageOf(
  id: string,
  runId: string,
  atMs: number,
  delivery: Delivery = Delivery.NEW_TURN,
  window: {readonly checkInId: string; readonly sinceMs: number} = {checkInId: 'ci', sinceMs: atMs},
): Message {
  return create(MessageSchema, {
    id,
    runId,
    toAgentId: 'lead',
    delivery,
    at: timestampFromMs(atMs),
    body: {
      case: 'catchUpRequest',
      value: create(CatchUpRequestSchema, {
        checkInId: window.checkInId,
        since: timestampFromMs(window.sinceMs),
        until: timestampFromMs(atMs),
      }),
    },
  });
}

/** A report a lead posted. */
export function reportMessageOf(
  id: string,
  runId: string,
  atMs: number,
  headline: string,
  changed = '',
  sequence = 1,
): Message {
  return create(MessageSchema, {
    id,
    runId,
    fromAgentId: 'lead',
    delivery: Delivery.CONSUMED,
    at: timestampFromMs(atMs),
    body: {
      case: 'report',
      value: create(ReportSchema, {headline, changed, kind: ReportKind.PROGRESS, sequence}),
    },
  });
}

/** One press of one viewer in a generated sequence, with how the host came to settle it. */
export interface Press {
  readonly viewer: string;
  /** How long after the previous press (of anyone), at least one millisecond. */
  readonly afterMs: number;
  readonly outcome: 'ready' | 'failed';
  /** Whether the lead was asked, and so a report may be linked later. */
  readonly asked: boolean;
}

export const pressArb: fc.Arbitrary<Press> = fc.record({
  viewer: fc.constantFrom('v1', 'v2', 'v3'),
  afterMs: fc.integer({min: 1, max: 3_600_000}),
  outcome: fc.constantFrom('ready' as const, 'failed' as const),
  asked: fc.boolean(),
});

/**
 * The check-ins a host would hold after these presses on a run that started at `startMs`, with
 * every copy each one went through (pending, then ready or failed, then report linked), in the
 * order the host committed them. A viewer's windows follow one another: a window starts where the
 * viewer's last one that did not fail ended, and the first at the start of the run.
 */
export function pressesToCopies(
  runId: string,
  startMs: number,
  presses: readonly Press[],
): {readonly copies: readonly CheckIn[]; readonly finals: readonly CheckIn[]} {
  const nextSince = new Map<string, number>();
  const copies: CheckIn[] = [];
  const finals: CheckIn[] = [];
  let now = startMs;
  presses.forEach((press, index) => {
    now += press.afterMs;
    const id = `ci-${index}`;
    const base = {
      id,
      runId,
      viewer: press.viewer,
      sinceMs: nextSince.get(press.viewer) ?? startMs,
      atMs: now,
      ...(press.asked ? {ask: `ask-${index}`} : {}),
    };
    const pending = checkInOf({...base, state: CheckInState.PENDING});
    const settled = checkInOf({
      ...base,
      state: press.outcome === 'ready' ? CheckInState.READY : CheckInState.FAILED,
    });
    const linked =
      press.asked && press.outcome === 'ready'
        ? checkInOf({...base, state: CheckInState.READY, report: `report-${index}`})
        : undefined;
    copies.push(pending, settled, ...(linked === undefined ? [] : [linked]));
    finals.push(linked ?? settled);
    if (press.outcome === 'ready') nextSince.set(press.viewer, now);
  });
  return {copies, finals};
}

/**
 * The facts of a busy window: spend across four types of agent with one unpriced turn, agents
 * started and ended, measurements, a throughput objective that improved and a latency one that
 * had not been measured before, plans created and changed. `reportMessageIds` names the reports
 * posted in the window.
 */
export function busyFacts(startMs: number, reportMessageIds: readonly string[] = []): WindowFacts {
  return windowFactsOf({
    roles: [
      roleSpend(AgentRole.LEAD, 4, 0, 1.25),
      roleSpend(AgentRole.PLANNER, 2, 0, 0.5),
      roleSpend(AgentRole.WORKER, 9, 1, 11.4),
      roleSpend(AgentRole.JUDGE, 1, 1, undefined),
    ],
    costUsd: 13.15,
    agentsStarted: 4,
    agentsEnded: 3,
    jobsMeasured: 7,
    objectives: [
      {
        metric: 'throughput',
        direction: ObjectiveDirection.MAXIMIZE,
        start: bestValue(1.84, startMs - 60_000, 'job-1'),
        end: bestValue(2.31, startMs + 60_000, 'job-7'),
      },
      {
        metric: 'p99 latency',
        direction: ObjectiveDirection.MINIMIZE,
        end: bestValue(118, startMs + 90_000, 'job-6'),
      },
    ],
    plansCreated: [31, 32],
    plansChanged: [26],
    reportMessageIds,
  });
}
