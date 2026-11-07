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
  EditCallSchema,
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
  OtherCallSchema,
  type Plan,
  PlannerHandoffSchema,
  ReadCallSchema,
  ReceiptSchema,
  ReportKind,
  ReportSchema,
  type RunChange,
  RunStatus,
  SearchCallSchema,
  SubagentSettledSchema,
  type ToolCall,
  type Turn,
  TurnStatus,
  TurnTrigger,
  type Usage,
  UsageSchema,
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
  /**
   * Agents spawn each other: two planners under the lead, workers under them (more than enough under
   * the first to fold), a helper spawned by a worker, a judge and a profiler; one worker fails. Steps
   * use every kind of tool call, long arguments and long outputs, and parents take a second turn
   * when their children report.
   */
  readonly tree?: boolean;
}

export interface Scenario {
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
type Step = (atMs: number, turnId: string, startedMs: number) => Item;

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
  readonly #spent = new Map<string, number>();

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
    extra: {
      output?: string;
      exit?: number;
      status?: ItemStatus;
      call?: ToolCall['call'];
    } = {},
  ): Step {
    return (atMs, turnId, startedMs) => {
      const ordinal = (this.#itemCount.get(agent.id) ?? 0) + 1;
      this.#itemCount.set(agent.id, ordinal);
      return itemOf({
        id: this.id('item'),
        turnId,
        agentId: agent.id,
        ordinal,
        atMs,
        startedMs,
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
      const before = at;
      at += this.rng.between(4, 40) * 1000;
      // The host first saw the item a little after the step before it ended.
      const item = step(at, open.id, before + Math.floor((at - before) / 3));
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

  /** What the agent's finished turns have cost so far, as the host would total it; absent before any. */
  spentBy(agent: Agent): Usage | undefined {
    const cost = this.#spent.get(agent.id);
    return cost === undefined ? undefined : create(UsageSchema, {costUsd: cost});
  }

  usage(agent: Agent): Usage {
    const claude = agent.harness === Harness.CLAUDE_CODE;
    const usage = usageOf({
      input: this.rng.between(2_000, 40_000),
      output: this.rng.between(300, 6_000),
      cacheRead: this.rng.between(10_000, 200_000),
      // A harness that does not report a number leaves it absent.
      cacheCreation: claude ? this.rng.between(1_000, 20_000) : undefined,
      reasoning: claude ? undefined : this.rng.between(100, 3_000),
      cost: claude ? this.rng.between(5, 90) / 100 : undefined,
    });
    if (usage.costUsd !== undefined)
      this.#spent.set(agent.id, (this.#spent.get(agent.id) ?? 0) + usage.costUsd);
    return usage;
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

const withSpent = (builder: Builder, agent: Agent): Agent => {
  const usage = builder.spentBy(agent);
  return usage === undefined ? agent : {...agent, usage};
};

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
  builder.putAgent(end, withSpent(builder, leadAgent(builder, AgentStatus.WAITING)));
  return end;
}

interface Sub {
  readonly agent: Agent;
  readonly endMs: number;
  /** The message the agent handed back to its parent, when it did. */
  readonly handoffId?: string;
  readonly parent: Agent;
}

/** Which role an agent has and which agent spawned it (an index into the slots, or -1 for the lead). */
interface Slot {
  readonly role: AgentRole;
  readonly parent: number;
}

function layoutOf(options: ScenarioOptions): Slot[] {
  if (options.tree !== true) {
    return ROLES.slice(0, Math.min(options.subagents, ROLES.length)).map(role => ({
      role,
      parent: -1,
    }));
  }
  const workers = Math.max(3, options.subagents - 5);
  const slots: Slot[] = [
    {role: AgentRole.PLANNER, parent: -1},
    {role: AgentRole.PLANNER, parent: -1},
  ];
  for (let at = 0; at < workers; at++) {
    slots.push({role: AgentRole.WORKER, parent: at < workers - 3 ? 0 : 1});
  }
  slots.push({role: AgentRole.WORKER, parent: slots.length - 1});
  slots.push({role: AgentRole.JUDGE, parent: -1}, {role: AgentRole.PROFILER, parent: -1});
  return slots;
}

function subagentOf(
  builder: Builder,
  index: number,
  startMs: number,
  slot: Slot,
  parent: Agent,
): Agent {
  const role = slot.role;
  const codex = index % 2 === 1;
  return create(AgentSchema, {
    id: `agent-${builder.options.seed}-${ROLE_NAME[role]}-${index + 1}`,
    runId: builder.runId,
    parentAgentId: parent.id,
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
    ...(builder.options.tree === true ? richSteps(builder, agent) : []),
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

const LONG_ARGUMENT = `python bench.py ${Array.from({length: 24}, (_, at) => `--variant tile-${64 + at * 8}-lds-${at}`).join(' ')}`;
const LONG_PATH = `src/decode/${'very-long-directory-name/'.repeat(6)}kernels/fused_attention_residual_epilogue.cc`;
const LONG_LINE = `trace ${'0123456789abcdef'.repeat(40)}`;

/** Every kind of tool call, long arguments and outputs, a failure, an empty output, and thinking that runs long. */
function richSteps(builder: Builder, agent: Agent): Step[] {
  const reasoning = [
    '**Plan.** The decode loop launches the router and the dispatch as two kernels.',
    '',
    '- fuse them so the expert ids stay in registers',
    '- check accuracy first, then bench at batch 32 and 64',
    '',
    'A 128 tile may not fit next to the router state, so a 64 tile is the safer variant. '.repeat(
      6,
    ),
  ].join('\n');
  return [
    builder.step(agent, 'reasoning', reasoning),
    builder.step(agent, 'tool', 'src/decode/loop.py', {
      call: {case: 'read', value: create(ReadCallSchema, {target: 'src/decode/loop.py'})},
      output: Array.from({length: 40}, (_, at) => `${at + 1}: def decode_step_${at}(batch):`).join(
        '\n',
      ),
      exit: 0,
    }),
    builder.step(agent, 'tool', LONG_PATH, {
      call: {case: 'read', value: create(ReadCallSchema, {target: LONG_PATH})},
      output: 'x = 1',
      exit: 0,
    }),
    builder.step(agent, 'tool', 'src/decode/loop.py', {
      call: {case: 'edit', value: create(EditCallSchema, {targets: ['src/decode/loop.py']})},
      output: 'applied 3 hunks',
      exit: 0,
    }),
    builder.step(agent, 'tool', 'decode_step', {
      call: {case: 'search', value: create(SearchCallSchema, {pattern: 'decode_step'})},
      output: 'src/decode.py:41\nsrc/decode/loop.py:7',
      exit: 0,
    }),
    builder.step(agent, 'tool', '', {
      call: {
        case: 'other',
        value: create(OtherCallSchema, {name: 'mcp__tools__bench', text: 'batch=64\ntrace=long'}),
      },
      output: '11,088 tok/s',
    }),
    builder.step(agent, 'tool', LONG_ARGUMENT, {
      output: [
        ...Array.from(
          {length: 160},
          (_, at) => `variant ${at}  ${1000 + at * 7} tok/s  p99 ${200 - at} ms`,
        ),
        LONG_LINE,
      ].join('\n'),
      exit: 0,
    }),
    builder.step(agent, 'tool', 'pytest -x tests/test_decode.py', {
      output: 'FAILED tests/test_decode.py::test_case_3 - AssertionError',
      exit: 1,
      status: ItemStatus.FAILED,
    }),
    builder.step(agent, 'tool', 'true', {output: '', exit: 0}),
    builder.step(
      agent,
      'assistant',
      `The fused kernel passes except \`test_case_3\`:\n\n1. the router state is read before it is written\n2. ${'unbroken'.repeat(40)}\n\n\`\`\`\nbatch=64  11,088 tok/s\n\`\`\``,
    ),
    builder.step(agent, 'assistant', ''),
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

function addSubagent(
  builder: Builder,
  index: number,
  after: number,
  tail: boolean,
  slot: Slot,
  parent: Agent,
): Sub {
  const startMs = after + builder.rng.between(5, 90) * 1000;
  const agent = subagentOf(builder, index, startMs, slot, parent);
  builder.putAgent(startMs, {...agent, status: AgentStatus.LAUNCHING});
  builder.putAgent(startMs + 1500, agent);
  const spawn = builder.message(startMs, {
    fromAgentId: parent.id,
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
  if (tail) return {agent, endMs: at, parent};
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
  if (builder.options.tree === true && index === 4) {
    // One worker fails: it settles without a handoff.
    builder.message(at + 1000, {
      toAgentId: parent.id,
      body: {
        case: 'subagentSettled',
        value: create(SubagentSettledSchema, {
          agentId: agent.id,
          status: AgentStatus.FAILED,
          detail: 'The session was lost.',
        }),
      },
    });
    builder.putAgent(at + 1000, {
      ...withSpent(builder, agent),
      status: AgentStatus.FAILED,
      endedAt: timestampFromMs(at + 1000),
      endDetail: 'The session was lost.',
    });
    return {agent, endMs: at + 1000, parent};
  }
  const handoff = handoffFor(agent.role, agent.planNumber, idea, index);
  const handed = builder.message(at, {
    fromAgentId: agent.id,
    toAgentId: parent.id,
    body: {case: 'handoff', value: handoff},
  });
  builder.putAgent(at + 1000, {
    ...withSpent(builder, agent),
    status: AgentStatus.IDLE,
    handedOff: true,
  });
  return {agent, endMs: at + 1000, handoffId: handed.id, parent};
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

/** In a tree run each parent takes a second turn, woken by the last of its children to hand back. */
function wakeParents(builder: Builder, lead: Agent, subs: readonly Sub[]): number[] {
  const parents = [lead, ...subs.map(sub => sub.agent)];
  return parents.flatMap(parent => {
    const children = subs.filter(sub => sub.parent.id === parent.id && sub.handoffId !== undefined);
    const last = children[children.length - 1];
    if (last?.handoffId === undefined) return [];
    const startMs = Math.max(...children.map(child => child.endMs)) + 3000;
    const woken = parent.id === lead.id ? withSpent(builder, parent) : parent;
    const end = builder.turn(
      woken,
      startMs,
      {kind: TurnTrigger.HANDOFF, messageId: last.handoffId},
      [
        builder.step(
          woken,
          'reasoning',
          'The children reported. Compare the claims before saying anything.',
        ),
        builder.step(
          woken,
          'assistant',
          `${children.length} children reported back. I kept the best idea.`,
        ),
      ],
    );
    builder.putAgent(end + 1000, {
      ...withSpent(builder, parent),
      status: parent.id === lead.id ? AgentStatus.WAITING : AgentStatus.IDLE,
    });
    return [end + 1000];
  });
}

export function generateScenario(options: ScenarioOptions): Scenario {
  const builder = new Builder(options);
  let now = startRun(builder);
  const subs: Sub[] = [];
  const slots = layoutOf(options);
  const count = slots.length;
  const lead = leadAgent(builder, AgentStatus.ACTIVE);
  slots.forEach((slot, index) => {
    const tail = options.live && index >= count - 2;
    const parent = slot.parent === -1 ? lead : (subs[slot.parent]?.agent ?? lead);
    subs.push(addSubagent(builder, index, now, tail, slot, parent));
    now += builder.rng.between(20, 60) * 1000;
  });
  const lastEnd = Math.max(
    now,
    ...subs.map(sub => sub.endMs),
    ...(options.tree === true ? wakeParents(builder, lead, subs) : []),
  );
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
