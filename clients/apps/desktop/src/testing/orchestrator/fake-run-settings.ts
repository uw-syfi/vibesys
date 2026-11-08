/**
 * How the fake host decides an `UpdateRun` and applies a waiting lead change: the facts it commits,
 * as the real core would (the run record, the agent when a lead switch applies, one `run_updated`
 * with every setting that changed), and the refusal when it takes none of it. Pure over the folded
 * state, so the daemon appends what it returns.
 */
import {create} from '@bufbuild/protobuf';
import {durationFromMs, timestampFromMs} from '@bufbuild/protobuf/wkt';
import {
  type Agent,
  AgentSchema,
  AgentSpecSchema,
  AgentStatus,
  CostLimitChangeSchema,
  FanoutChangeSchema,
  LeadChangeSchema,
  LeadChangeState,
  oneofIs,
  PendingLeadSchema,
  Refusal,
  type Run,
  type RunChange,
  RunLimit,
  RunSchema,
  RunStatus,
  RunUpdatedSchema,
  type SettingChange,
  SettingChangeSchema,
  TimeLimitChangeSchema,
  type UpdateRun,
} from '@vibesys/orchestrator-proto';
import type {OrchestratorState} from '../../state/orchestrator/state.js';
import {ms} from '../../state/orchestrator/time.js';

type Body = RunChange['body'];

/** What the host does with an update: the facts to commit, or the refusal that commits none of them. */
export interface UpdateDecision {
  readonly refusal: Refusal;
  /** For `MODEL_NOT_ALLOWED`: the model refused. */
  readonly model: string;
  /** For `MODEL_NOT_ALLOWED`: the models the host allows. */
  readonly allowedModels: readonly string[];
  readonly bodies: readonly Body[];
}

const refused = (refusal: Refusal, model = '', allowedModels: readonly string[] = []) =>
  ({refusal, model, allowedModels, bodies: []}) satisfies UpdateDecision;

const LIVE = new Set([RunStatus.STARTING, RunStatus.RUNNING]);

const settingOf = (change: SettingChange['change']): SettingChange =>
  create(SettingChangeSchema, {change});

/** The run whose limits and fanout the update sets, and the changes that made. */
function limitChanges(run: Run, update: UpdateRun): {run: Run; changes: SettingChange[]} {
  const changes: SettingChange[] = [];
  let next = run;
  for (const step of [costStep, timeStep, fanoutStep]) {
    const done = step(next, update);
    next = done.run;
    if (done.change !== undefined) changes.push(done.change);
  }
  return {run: next, changes};
}

/** One setting's step: the run with it set, and the change it made; none when nothing differs. */
interface Step {
  readonly run: Run;
  readonly change?: SettingChange;
}

function costStep(run: Run, update: UpdateRun): Step {
  const {costLimit} = update;
  if (costLimit === undefined) return {run};
  const change = costLimit.change;
  const to = oneofIs(change, 'setUsd', {setUsd: true, remove: true}) ? change.value : undefined;
  if (to === run.maxCostUsd) return {run};
  const value = create(CostLimitChangeSchema, {
    ...(run.maxCostUsd === undefined ? {} : {from: run.maxCostUsd}),
    ...(to === undefined ? {} : {to}),
  });
  return {
    run: create(RunSchema, {...run, maxCostUsd: to}),
    change: settingOf({case: 'costLimit', value}),
  };
}

function timeStep(run: Run, update: UpdateRun): Step {
  const {timeLimit} = update;
  if (timeLimit === undefined) return {run};
  const start = ms(run.optimizeStartedAt) ?? ms(run.createdAt) ?? 0;
  const was = ms(run.deadline);
  const change = timeLimit.change;
  const seconds = oneofIs(change, 'set', {set: true, remove: true})
    ? change.value.seconds
    : undefined;
  const deadline = seconds === undefined ? undefined : start + Number(seconds) * 1000;
  if (deadline === (was ?? undefined)) return {run};
  const value = create(TimeLimitChangeSchema, {
    from: was === null ? undefined : durationFromMs(was - start),
    to: deadline === undefined ? undefined : durationFromMs(deadline - start),
  });
  return {
    run: create(RunSchema, {
      ...run,
      deadline: deadline === undefined ? undefined : timestampFromMs(deadline),
    }),
    change: settingOf({case: 'timeLimit', value}),
  };
}

function fanoutStep(run: Run, update: UpdateRun): Step {
  const {fanoutLimit} = update;
  if (fanoutLimit === undefined || fanoutLimit === run.fanoutLimit) return {run};
  const value = create(FanoutChangeSchema, {from: run.fanoutLimit, to: fanoutLimit});
  return {
    run: create(RunSchema, {...run, fanoutLimit}),
    change: settingOf({case: 'fanout', value}),
  };
}

const specOf = (agent: Agent) =>
  create(AgentSpecSchema, {
    harness: agent.harness,
    model: agent.model,
    reasoningEffort: agent.reasoningEffort,
  });

const updated = (runId: string, changes: SettingChange[]): Body => ({
  case: 'runUpdated',
  value: create(RunUpdatedSchema, {runId, changes}),
});

/** A lead switch: applied at once when the lead rests, else pending until `applyPendingLead`. */
function leadFacts(
  run: Run,
  lead: Agent,
  to: NonNullable<UpdateRun['lead']>,
): {run: Run; agent: Agent | undefined; change: SettingChange} {
  const from = specOf(lead);
  const resting = lead.status !== AgentStatus.ACTIVE && lead.status !== AgentStatus.LAUNCHING;
  const state = resting ? LeadChangeState.APPLIED : LeadChangeState.PENDING;
  const change = settingOf({case: 'lead', value: create(LeadChangeSchema, {from, to, state})});
  if (!resting) {
    const pending = create(PendingLeadSchema, {
      harness: to.harness,
      model: to.model,
      reasoningEffort: to.reasoningEffort,
    });
    return {run: create(RunSchema, {...run, pendingLead: pending}), agent: undefined, change};
  }
  const agent = create(AgentSchema, {
    ...lead,
    harness: to.harness,
    model: to.model,
    reasoningEffort: to.reasoningEffort,
  });
  return {run: create(RunSchema, {...run, pendingLead: undefined}), agent, change};
}

/** The reason a live run takes no change to a limit that it already reached, or none. */
function limitReached(run: Run, update: UpdateRun): Refusal {
  if (update.costLimit !== undefined && run.limitReached === RunLimit.COST)
    return Refusal.COST_LIMIT_REACHED;
  if (update.timeLimit !== undefined && run.limitReached === RunLimit.TIME)
    return Refusal.TIME_LIMIT_REACHED;
  return Refusal.UNSPECIFIED;
}

/**
 * An update as the host decides it: refused whole when the run is gone or over, has reached a limit
 * the update changes, or the lead's model is outside the allowed list; else every setting that
 * differs is committed together. An update that changes nothing commits nothing.
 */
export function decideUpdate(
  state: OrchestratorState,
  update: UpdateRun,
  allowedModels: readonly string[],
): UpdateDecision {
  const run = state.runs[update.runId];
  if (run === undefined) return refused(Refusal.RUN_NOT_FOUND);
  if (!LIVE.has(run.status)) return refused(Refusal.RUN_NOT_RUNNING);
  const reached = limitReached(run, update);
  if (reached !== Refusal.UNSPECIFIED) return refused(reached);
  const {lead: to} = update;
  if (to !== undefined && allowedModels.length > 0 && !allowedModels.includes(to.model))
    return refused(Refusal.MODEL_NOT_ALLOWED, to.model, allowedModels);
  const {run: limited, changes} = limitChanges(run, update);
  const lead = state.agents[run.leadAgentId];
  const asked = to !== undefined && lead !== undefined;
  const switched = asked ? leadFacts(limited, lead, to) : undefined;
  const bodies: Body[] = [];
  const final = switched?.run ?? limited;
  if (changes.length > 0 || switched !== undefined) bodies.push({case: 'run', value: final});
  if (switched?.agent !== undefined) bodies.push({case: 'agent', value: switched.agent});
  const all = switched === undefined ? changes : [...changes, switched.change];
  if (all.length > 0) bodies.push(updated(run.id, all));
  return {refusal: Refusal.UNSPECIFIED, model: '', allowedModels: [], bodies};
}

/**
 * The lead's turn ended and a lead change was waiting: the lead becomes the pending one and the
 * run says so. Nothing is committed for a run with no change waiting.
 */
export function applyPendingLead(state: OrchestratorState, runId: string): readonly Body[] {
  const run = state.runs[runId];
  const lead = run === undefined ? undefined : state.agents[run.leadAgentId];
  const pending = run?.pendingLead;
  if (run === undefined || lead === undefined || pending === undefined) return [];
  const to = create(AgentSpecSchema, {
    harness: pending.harness,
    model: pending.model,
    reasoningEffort: pending.reasoningEffort,
  });
  const change = settingOf({
    case: 'lead',
    value: create(LeadChangeSchema, {from: specOf(lead), to, state: LeadChangeState.APPLIED}),
  });
  return [
    {case: 'run', value: create(RunSchema, {...run, pendingLead: undefined})},
    {
      case: 'agent',
      value: create(AgentSchema, {
        ...lead,
        harness: to.harness,
        model: to.model,
        reasoningEffort: to.reasoningEffort,
      }),
    },
    updated(run.id, [change]),
  ];
}
