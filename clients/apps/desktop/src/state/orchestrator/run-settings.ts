/**
 * A live run's settings as the screens read them: what each limit and the lead are now, what the run
 * has used against them, and the timeline of the changes made while it ran. Plain selectors over
 * the fold's state; the host's run record is the authority for the values, and the timeline is what
 * its `run_updated` events said.
 */
import {
  AgentRole,
  type AgentSpec,
  AgentStatus,
  type Harness,
  LeadChangeState,
  type PendingLead,
  type Run,
  type SettingChange,
} from '@vibesys/orchestrator-proto';
import type {OrchestratorState, SettingsRecord} from './state.js';
import {ms} from './time.js';

/** The lead's tool, model and effort. An empty model is the tool's own default. */
export interface LeadSetting {
  readonly harness: Harness;
  readonly model: string;
  readonly effort: string;
}

/** One setting that changed, with each side as the host said it; null is "no limit" or "no lead known". */
export type Change =
  | {readonly kind: 'cost'; readonly from: number | null; readonly to: number | null}
  | {readonly kind: 'time'; readonly from: number | null; readonly to: number | null}
  | {readonly kind: 'fanout'; readonly from: number; readonly to: number}
  | {
      readonly kind: 'lead';
      readonly from: LeadSetting | null;
      readonly to: LeadSetting;
      readonly state: 'pending' | 'applied';
    };

/** One entry of the timeline: every setting one update changed. */
export interface TimelineRow {
  readonly sequence: bigint;
  readonly atMs: number;
  readonly changes: readonly Change[];
}

const leadOf = (spec: AgentSpec | undefined): LeadSetting | null =>
  spec === undefined
    ? null
    : {harness: spec.harness, model: spec.model, effort: spec.reasoningEffort};

const seconds = (duration: {readonly seconds: bigint} | undefined): number | null =>
  duration === undefined ? null : Number(duration.seconds);

/** A change as the screens read it, or null for an arm this client does not know. */
export function changeOf(change: SettingChange): Change | null {
  const {change: body} = change;
  switch (body.case) {
    case 'costLimit':
      return {kind: 'cost', from: body.value.from ?? null, to: body.value.to ?? null};
    case 'timeLimit':
      return {kind: 'time', from: seconds(body.value.from), to: seconds(body.value.to)};
    case 'fanout':
      return {kind: 'fanout', from: body.value.from, to: body.value.to};
    case 'lead': {
      const to = leadOf(body.value.to);
      return to === null
        ? null
        : {
            kind: 'lead',
            from: leadOf(body.value.from),
            to,
            state: body.value.state === LeadChangeState.PENDING ? 'pending' : 'applied',
          };
    }
    case undefined:
      return null;
    default:
      body satisfies never;
      return null;
  }
}

/** The run's settings changes, newest first. */
export function settingsTimeline(state: OrchestratorState, runId: string): readonly TimelineRow[] {
  const held: readonly SettingsRecord[] = state.settingChanges[runId] ?? [];
  return held
    .map(record => ({
      sequence: record.sequence,
      atMs: record.atMs,
      changes: record.update.changes.flatMap(change => changeOf(change) ?? []),
    }))
    .reverse();
}

/**
 * What the changes last set, by setting: the value of the newest change of each kind. A setting no
 * change touched is absent. `time` and `cost` are null for a limit that was removed.
 */
export interface LastWrite {
  readonly cost?: number | null;
  readonly time?: number | null;
  readonly fanout?: number;
  readonly lead?: LeadSetting;
}

/** The last-write state of a timeline given oldest first: each kind is what its newest change set. */
export function lastWrite(oldestFirst: readonly Pick<TimelineRow, 'changes'>[]): LastWrite {
  let written: LastWrite = {};
  for (const row of oldestFirst) {
    for (const change of row.changes) {
      switch (change.kind) {
        case 'cost':
          written = {...written, cost: change.to};
          break;
        case 'time':
          written = {...written, time: change.to};
          break;
        case 'fanout':
          written = {...written, fanout: change.to};
          break;
        case 'lead':
          written = {...written, lead: change.to};
          break;
      }
    }
  }
  return written;
}

/** The settings of the run in oldest-first order, the form `lastWrite` reads. */
export const settingsHistory = (state: OrchestratorState, runId: string): readonly TimelineRow[] =>
  [...settingsTimeline(state, runId)].reverse();

/** What the run has used of the cost limit, and what its setup spent apart from it. */
export interface CostSetting {
  readonly limitUsd: number | null;
  /** The run's spend counted against the limit: all of it, less what setup cost. */
  readonly spentUsd: number | null;
  /** What the setup phase cost, kept apart from the limit; null for a run with no setup. */
  readonly setupUsd: number | null;
}

export interface TimeSetting {
  /** The total allowed, counted from where the run's own work began; null for no limit. */
  readonly limitSeconds: number | null;
  /** How long the run's work has gone, from the same start; null when the run has not begun. */
  readonly elapsedSeconds: number | null;
  /** How long setup took, kept apart from the limit; null for a run with no setup. */
  readonly setupSeconds: number | null;
}

export interface FanoutSetting {
  readonly cap: number;
  /** How many agents other than the lead are live; null until the run's agents have been read. */
  readonly live: number | null;
  /** How many live agents are above the cap: lowering the cap stops none, so they run on. */
  readonly above: number;
}

export interface LeadState {
  /** The lead as it runs now; null until the run's agents have been read. */
  readonly current: LeadSetting | null;
  /** The lead that takes over when the current lead's turn ends; null when none waits. */
  readonly pending: LeadSetting | null;
}

export interface RunSettings {
  readonly cost: CostSetting;
  readonly time: TimeSetting;
  readonly fanout: FanoutSetting;
  readonly lead: LeadState;
}

const pendingOf = (pending: PendingLead | undefined): LeadSetting | null =>
  pending === undefined
    ? null
    : {harness: pending.harness, model: pending.model, effort: pending.reasoningEffort};

const OVER = new Set([AgentStatus.ENDED, AgentStatus.FAILED, AgentStatus.LOST]);

const secondsBetween = (from: number | null, to: number | null): number | null =>
  from === null || to === null ? null : Math.max(0, Math.round((to - from) / 1000));

function costSetting(run: Run): CostSetting {
  const used = run.usage?.costUsd ?? null;
  const setupUsd = run.setupUsage === undefined ? null : (run.setupUsage.costUsd ?? 0);
  return {
    limitUsd: run.maxCostUsd ?? null,
    spentUsd: used === null ? null : Math.max(0, used - (setupUsd ?? 0)),
    setupUsd,
  };
}

function timeSetting(run: Run, nowMs: number): TimeSetting {
  const createdMs = ms(run.createdAt);
  const optimizeMs = ms(run.optimizeStartedAt);
  const startMs = optimizeMs ?? createdMs;
  return {
    limitSeconds: secondsBetween(startMs, ms(run.deadline)),
    elapsedSeconds: secondsBetween(startMs, ms(run.endedAt) ?? nowMs),
    setupSeconds: secondsBetween(createdMs, optimizeMs),
  };
}

/** The agents of `runId` other than its lead that are not over: what the cap limits. */
function liveSubagents(state: OrchestratorState, runId: string): number | null {
  if (state.histories[runId] === undefined) return null;
  return Object.values(state.agents).filter(
    agent => agent.runId === runId && agent.role !== AgentRole.LEAD && !OVER.has(agent.status),
  ).length;
}

function fanoutSetting(state: OrchestratorState, run: Run): FanoutSetting {
  const live = liveSubagents(state, run.id);
  return {
    cap: run.fanoutLimit,
    live,
    above: live === null ? 0 : Math.max(0, live - run.fanoutLimit),
  };
}

function leadState(state: OrchestratorState, run: Run): LeadState {
  const lead = state.agents[run.leadAgentId];
  return {
    current:
      lead === undefined
        ? null
        : {harness: lead.harness, model: lead.model, effort: lead.reasoningEffort},
    pending: pendingOf(run.pendingLead),
  };
}

/** The settings of a run as its record says, with what it has used; undefined for a run not held. */
export function runSettingsOf(
  state: OrchestratorState,
  runId: string,
  nowMs: number,
): RunSettings | undefined {
  const run = state.runs[runId];
  if (run === undefined) return undefined;
  return {
    cost: costSetting(run),
    time: timeSetting(run, nowMs),
    fanout: fanoutSetting(state, run),
    lead: leadState(state, run),
  };
}
