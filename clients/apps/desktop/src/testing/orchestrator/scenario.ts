/**
 * A realistic orchestrated run as a gap-free stream of changes: a lead that plans, workers that try
 * ideas and submit jobs, a judge, and a combiner, on a mix of the two harnesses, with operator
 * messages and their receipts, handoffs, a report, and an unfinished tail so the run is live.
 * A function of its options alone (seeded), so the same options give the same bytes.
 */
import {create} from '@bufbuild/protobuf';
import {timestampFromMs} from '@bufbuild/protobuf/wkt';
import {
  type Agent,
  AgentRole,
  AgentSchema,
  AgentStatus,
  AgentStopStage,
  CandidateSchema,
  CapabilitiesSchema,
  Delivery,
  type Delta,
  DeltaSchema,
  HandoffSchema,
  Harness,
  type Item,
  ItemStatus,
  JobFinishedSchema,
  JobOutcome,
  JudgeHandoffSchema,
  JudgeVerdict,
  type Message,
  MessageSchema,
  NoCandidateSchema,
  type Plan,
  PlannerHandoffSchema,
  ReceiptSchema,
  ReportKind,
  ReportSchema,
  type RunChange,
  RunStatus,
  type SearchState,
  type Turn,
  TurnStatus,
  TurnTrigger,
  type Usage,
  WorkerHandoffSchema,
} from '@vibesys/orchestrator-proto';
import {itemOf, planOf, runOf, turnOf, usageOf} from './builders.js';
import {ChangeLog, Rng} from './log.js';

export interface ScenarioOptions {
  readonly seed: number;
  /** Agents besides the lead. */
  readonly subagents: number;
  /** Leave the last turns unfinished, so the run is live at the end. */
  readonly live: boolean;
  /** Extra read-and-try steps in each subagent's first turn, to make long histories (default 0). */
  readonly depth?: number;
}

export interface Scenario {
  /** The search facts of every run the scenario holds, when it holds several (`search` serves one). */
  readonly searches?: readonly SearchState[];
  readonly search?: boolean;
  readonly searchStale?: boolean;
  readonly searchTruncated?: boolean;
  readonly runId: string;
  readonly changes: readonly RunChange[];
  readonly plans: readonly Plan[];
  /** Progress on running items, which the daemon sends but never stores. */
  readonly deltas: readonly Delta[];
  /** The time of the last change. */
  readonly endMs: number;
}

const START_MS = Date.UTC(2026, 10, 1, 17, 0, 0);
const MINUTE = 60_000;

const ROLES = [
  AgentRole.PLANNER,
  AgentRole.PLANNER,
  AgentRole.WORKER,
  AgentRole.WORKER,
  AgentRole.WORKER,
  AgentRole.WORKER,
  AgentRole.JUDGE,
  AgentRole.WORKER,
  AgentRole.COMBINER,
  AgentRole.PROFILER,
] as const;

const ROLE_NAME: Record<number, string> = {
  [AgentRole.PLANNER]: 'planner',
  [AgentRole.WORKER]: 'worker',
  [AgentRole.JUDGE]: 'judge',
  [AgentRole.COMBINER]: 'combiner',
  [AgentRole.PROFILER]: 'profiler',
};

const IDEAS = [
  'Cache the tokenizer output between requests',
  'Fuse the attention and residual kernels',
  'Batch small decode steps together',
  'Move the sampler to the GPU',
  'Pin the weights in host memory',
  'Overlap copies with compute',
  'Use a paged cache for long prompts',
  'Skip the second layer norm on the last token',
];

/** One step of a turn: builds the item that happens `atMs` into turn `turnId`. */
type Step = (atMs: number, turnId: string) => Item;

class Builder {
  readonly rng: Rng;
  readonly runId: string;
  readonly log: ChangeLog;
  readonly deltas: Delta[] = [];
  readonly plans: Plan[] = [];
  readonly leadId: string;
  readonly #itemCount = new Map<string, number>();
  readonly #turnCount = new Map<string, number>();
  #counter = 0;

  constructor(readonly options: ScenarioOptions) {
    this.rng = new Rng(options.seed);
    this.runId = `run-${options.seed}`;
    this.log = new ChangeLog(this.runId);
    this.leadId = `agent-${options.seed}-lead`;
  }

  id(prefix: string): string {
    this.#counter += 1;
    return `${prefix}-${this.options.seed}-${this.#counter}`;
  }

  putAgent(atMs: number, agent: Agent): void {
    this.log.push(atMs, {case: 'agent', value: agent});
  }

  message(
    atMs: number,
    fields: {fromAgentId?: string; toAgentId?: string; delivery?: Delivery; body: Message['body']},
  ): Message {
    const value = create(MessageSchema, {
      id: this.id('msg'),
      runId: this.runId,
      delivery: Delivery.CONSUMED,
      at: timestampFromMs(atMs),
      ...fields,
    });
    this.log.push(atMs, {case: 'message', value});
    return value;
  }

  /** A step that makes one item of `agent`. */
  step(
    agent: Agent,
    kind: 'prompt' | 'reasoning' | 'assistant' | 'tool',
    text: string,
    extra: {output?: string; exit?: number; status?: ItemStatus} = {},
  ): Step {
    return (atMs, turnId) => {
      const ordinal = (this.#itemCount.get(agent.id) ?? 0) + 1;
      this.#itemCount.set(agent.id, ordinal);
      return itemOf({
        id: this.id('item'),
        turnId,
        agentId: agent.id,
        ordinal,
        atMs,
        kind,
        text,
        ...extra,
      });
    };
  }

  /** One turn of `agent`: its items, then (unless `running`) the close with its cost. Returns its end. */
  turn(
    agent: Agent,
    startMs: number,
    trigger: {kind: TurnTrigger; messageId: string},
    steps: readonly Step[],
    running = false,
  ): number {
    const ordinal = (this.#turnCount.get(agent.id) ?? 0) + 1;
    this.#turnCount.set(agent.id, ordinal);
    const open: Turn = turnOf({
      id: this.id('turn'),
      agentId: agent.id,
      ordinal,
      trigger: trigger.kind,
      triggerMessageId: trigger.messageId,
      startMs,
      status: TurnStatus.RUNNING,
    });
    this.log.push(startMs, {case: 'turn', value: open});
    let at = startMs;
    for (const step of steps) {
      at += this.rng.between(4, 40) * 1000;
      const item = step(at, open.id);
      this.log.push(at, {case: 'item', value: item});
      if (item.status === ItemStatus.RUNNING) this.progress(agent.id, item.id);
    }
    if (running) return at;
    at += this.rng.between(2, 15) * 1000;
    const closed = {
      ...open,
      status: TurnStatus.COMPLETED,
      endedAt: timestampFromMs(at),
      usage: this.usage(agent),
    };
    this.log.push(at, {case: 'turn', value: closed});
    return at;
  }

  progress(agentId: string, itemId: string): void {
    for (const text of ['Looking at ', 'the profile ', 'for the next ', 'step']) {
      this.deltas.push(create(DeltaSchema, {agentId, itemId, body: {case: 'text', value: text}}));
    }
  }

  usage(agent: Agent): Usage {
    const claude = agent.harness === Harness.CLAUDE_CODE;
    return usageOf({
      input: this.rng.between(2_000, 40_000),
      output: this.rng.between(300, 6_000),
      cacheRead: this.rng.between(10_000, 200_000),
      // A harness that does not report a number leaves it absent.
      cacheCreation: claude ? this.rng.between(1_000, 20_000) : undefined,
      reasoning: claude ? undefined : this.rng.between(100, 3_000),
      cost: claude ? this.rng.between(5, 90) / 100 : undefined,
    });
  }
}

function leadAgent(builder: Builder, status: AgentStatus): Agent {
  return create(AgentSchema, {
    id: builder.leadId,
    runId: builder.runId,
    role: AgentRole.LEAD,
    harness: Harness.CLAUDE_CODE,
    stopStage: AgentStopStage.NONE,
    session: 1,
    model: 'model-a',
    reasoningEffort: 'high',
    status,
    startedAt: timestampFromMs(START_MS),
    capabilities: create(CapabilitiesSchema, {steers: true, resumes: true}),
  });
}

/** The lead opens the run and plans; returns when its first turn ends. */
function startRun(builder: Builder): number {
  const {runId, leadId, log} = builder;
  const run = (status: RunStatus) =>
    runOf({id: runId, leadAgentId: leadId, status, startMs: START_MS, fanout: 5});
  log.push(START_MS, {case: 'run', value: run(RunStatus.STARTING)});
  builder.putAgent(START_MS, leadAgent(builder, AgentStatus.LAUNCHING));
  log.push(START_MS + 2000, {case: 'run', value: run(RunStatus.RUNNING)});
  builder.putAgent(START_MS + 2000, leadAgent(builder, AgentStatus.ACTIVE));
  const text = 'Make the decode loop faster without changing its output.';
  const brief = builder.message(START_MS + 2000, {
    toAgentId: leadId,
    body: {case: 'brief', value: text},
  });
  const lead = leadAgent(builder, AgentStatus.ACTIVE);
  const end = builder.turn(lead, START_MS + 3000, {kind: TurnTrigger.BRIEF, messageId: brief.id}, [
    builder.step(lead, 'prompt', text),
    builder.step(
      lead,
      'reasoning',
      'I will read the plans first, then ask planners for ideas and workers to try the best ones.',
    ),
    builder.step(lead, 'tool', 'vibesys-cli plan list', {output: 'No ideas yet.', exit: 0}),
    builder.step(
      lead,
      'assistant',
      'No ideas exist yet. I am asking two planners to propose some.',
    ),
  ]);
  builder.putAgent(end, leadAgent(builder, AgentStatus.WAITING));
  return end;
}

interface Sub {
  readonly agent: Agent;
  readonly endMs: number;
}

function subagentOf(builder: Builder, index: number, startMs: number): Agent {
  const role = ROLES[index] ?? AgentRole.WORKER;
  const codex = index % 2 === 1;
  return create(AgentSchema, {
    id: `agent-${builder.options.seed}-${ROLE_NAME[role]}-${index + 1}`,
    runId: builder.runId,
    parentAgentId: builder.leadId,
    role,
    harness: codex ? Harness.CODEX : Harness.CLAUDE_CODE,
    stopStage: AgentStopStage.NONE,
    session: 1,
    model: codex ? 'model-c' : 'model-b',
    reasoningEffort: codex ? 'medium' : 'high',
    planNumber: role === AgentRole.WORKER ? builder.plans.length + 1 : 0,
    status: AgentStatus.ACTIVE,
    startedAt: timestampFromMs(startMs),
    capabilities: create(CapabilitiesSchema, {steers: !codex, resumes: true}),
  });
}

/** What a subagent does in its first turn, as steps. */
function workSteps(
  builder: Builder,
  agent: Agent,
  idea: string,
  jobId: string,
  tail: boolean,
): Step[] {
  const role = ROLE_NAME[agent.role] ?? 'worker';
  const worker = agent.role === AgentRole.WORKER;
  return [
    builder.step(agent, 'prompt', `Work as the ${role}. ${worker ? idea : ''}`.trim()),
    builder.step(agent, 'reasoning', 'Reading the code before changing anything.'),
    builder.step(agent, 'tool', 'rg -n "decode_step" src', {
      output: 'src/decode.py:41: def decode_step(...)',
      exit: 0,
    }),
    ...investigation(builder, agent, builder.options.depth ?? 0),
    ...(worker
      ? [
          builder.step(agent, 'tool', 'python bench.py', {
            output: 'throughput 1182 tokens/s',
            exit: 0,
          }),
          builder.step(agent, 'tool', `vibesys-cli job submit bench --label ${jobId}`, {
            output: `submitted ${jobId}`,
            exit: 0,
          }),
        ]
      : []),
    builder.step(
      agent,
      'assistant',
      tail ? 'Waiting for the benchmark to finish.' : 'Done. I will hand off now.',
    ),
    ...(tail
      ? [builder.step(agent, 'tool', 'tail -f bench.log', {output: '', status: ItemStatus.RUNNING})]
      : []),
  ];
}

/** `depth` more rounds of thinking and trying, so an agent's history can be long. */
function investigation(builder: Builder, agent: Agent, depth: number): Step[] {
  const steps: Step[] = [];
  for (let round = 1; round <= depth; round++) {
    steps.push(
      builder.step(agent, 'reasoning', `Round ${round}: checking whether the change is safe.`),
      builder.step(agent, 'tool', `pytest tests/test_decode.py -k case_${round}`, {
        output: `${round} passed`,
        exit: 0,
      }),
    );
  }
  return steps;
}

function addSubagent(builder: Builder, index: number, after: number, tail: boolean): Sub {
  const startMs = after + builder.rng.between(5, 90) * 1000;
  const agent = subagentOf(builder, index, startMs);
  builder.putAgent(startMs, {...agent, status: AgentStatus.LAUNCHING});
  builder.putAgent(startMs + 1500, agent);
  const spawn = builder.message(startMs, {
    fromAgentId: builder.leadId,
    toAgentId: agent.id,
    body: {case: 'brief', value: `Work as the ${ROLE_NAME[agent.role]}.`},
  });
  const idea = builder.rng.pick(IDEAS);
  if (agent.role === AgentRole.WORKER) {
    const status = [3, 5, 1, 2, 4][builder.plans.length % 5] ?? 1;
    builder.plans.push(planOf(agent.planNumber, idea, status, startMs));
  }
  const jobId = `job-${builder.options.seed}-${index + 1}`;
  let at = builder.turn(
    agent,
    startMs + 1500,
    {kind: TurnTrigger.BRIEF, messageId: spawn.id},
    workSteps(builder, agent, idea, jobId, tail),
    tail,
  );
  if (tail) return {agent, endMs: at};
  if (agent.role === AgentRole.WORKER) {
    const failed = index % 4 === 3;
    const jobEnd = at + builder.rng.between(1, 4) * MINUTE;
    builder.message(jobEnd, {
      toAgentId: agent.id,
      body: {
        case: 'jobFinished',
        value: create(JobFinishedSchema, {
          jobId,
          outcome: failed ? JobOutcome.FAILED : JobOutcome.SUCCEEDED,
          submittedByAgentId: agent.id,
          detail: failed ? 'The benchmark ran out of memory.' : 'Finished in 3 minutes.',
        }),
      },
    });
    at = Math.max(at, jobEnd);
  }
  const handoff = handoffFor(agent.role, agent.planNumber, idea, index);
  builder.message(at, {
    fromAgentId: agent.id,
    toAgentId: builder.leadId,
    body: {case: 'handoff', value: handoff},
  });
  builder.putAgent(at + 1000, {...agent, status: AgentStatus.IDLE, handedOff: true});
  return {agent, endMs: at + 1000};
}

/** The operator writes to a worker and the daemon answers with a receipt. */
function operatorMessage(builder: Builder, target: Agent, atMs: number): void {
  const sent = builder.message(atMs, {
    toAgentId: target.id,
    delivery: Delivery.QUEUED,
    body: {case: 'text', value: 'Please also check the memory use.'},
  });
  const receipt = create(ReceiptSchema, {
    commandId: 'cmd-operator-1',
    runId: builder.runId,
    agentId: target.id,
    messageId: sent.id,
    delivery: Delivery.QUEUED,
    sequence: BigInt(builder.log.changes.length + 1),
  });
  builder.log.push(atMs, {case: 'receipt', value: receipt});
}

function finishRun(builder: Builder, atMs: number): void {
  const report = create(ReportSchema, {
    kind: ReportKind.FINAL,
    sequence: 1,
    headline: 'Batching the decode steps made it 18 percent faster.',
    changed: 'Decode steps now run in batches of eight.',
    decisions: 'We kept the batching idea and dropped the cache idea.',
    openForUser: 'Do you want the paged cache tried next?',
  });
  builder.message(atMs, {fromAgentId: builder.leadId, body: {case: 'report', value: report}});
  const ended = {
    ...runOf({
      id: builder.runId,
      leadAgentId: builder.leadId,
      status: RunStatus.ENDED,
      startMs: START_MS,
      fanout: 5,
    }),
    endedAt: timestampFromMs(atMs + 1000),
  };
  builder.log.push(atMs + 1000, {case: 'run', value: ended});
}

export function generateScenario(options: ScenarioOptions): Scenario {
  const builder = new Builder(options);
  let now = startRun(builder);
  const subs: Sub[] = [];
  const count = Math.min(options.subagents, ROLES.length);
  for (let index = 0; index < count; index++) {
    const tail = options.live && index >= count - 2;
    subs.push(addSubagent(builder, index, now, tail));
    now += builder.rng.between(20, 60) * 1000;
  }
  const lastEnd = Math.max(now, ...subs.map(sub => sub.endMs));
  const target = subs.find(sub => sub.agent.role === AgentRole.WORKER)?.agent;
  if (target !== undefined) operatorMessage(builder, target, lastEnd + 5000);
  if (!options.live) finishRun(builder, lastEnd + 30_000);
  const last = builder.log.changes[builder.log.changes.length - 1];
  const endMs = last?.at === undefined ? START_MS : Number(last.at.seconds) * 1000;
  return {
    runId: builder.runId,
    changes: builder.log.changes,
    plans: builder.plans,
    deltas: builder.deltas,
    endMs,
  };
}

function handoffFor(role: AgentRole, planNumber: number, idea: string, index: number) {
  const common = {
    planNumber,
    claim:
      role === AgentRole.WORKER
        ? `${idea} makes decoding ${6 + index} percent faster.`
        : role === AgentRole.JUDGE
          ? 'The batching change holds up under review.'
          : 'Ideas are written down.',
    learned: 'The step is memory bound.',
    next: 'Try a larger batch.',
  };
  if (role === AgentRole.WORKER) {
    return create(HandoffSchema, {
      ...common,
      body: {
        case: 'worker',
        value: create(WorkerHandoffSchema, {
          outcome:
            index % 4 !== 3
              ? {
                  case: 'candidate',
                  value: create(CandidateSchema, {
                    commit: `c${index}f3a9e1`,
                    parentCommit: 'b81d02a',
                    branch: `idea-${planNumber}`,
                    checks: 'All unit tests pass.',
                    measurement: `1182 to ${1182 + 40 * (index + 1)} tokens per second over five runs.`,
                  }),
                }
              : {
                  case: 'noCandidate',
                  value: create(NoCandidateSchema, {
                    blocker: 'The batched kernel did not compile.',
                  }),
                },
        }),
      },
    });
  }
  if (role === AgentRole.JUDGE) {
    return create(HandoffSchema, {
      ...common,
      body: {
        case: 'judge',
        value: create(JudgeHandoffSchema, {
          commit: 'c0f3a9e1',
          verdict: JudgeVerdict.PASS,
          reasons: 'The comparison says better and the change is on the measured path.',
        }),
      },
    });
  }
  return create(HandoffSchema, {
    ...common,
    body: {
      case: 'planner',
      value: create(PlannerHandoffSchema, {plan: `plans/plan-${planNumber}.md`}),
    },
  });
}
