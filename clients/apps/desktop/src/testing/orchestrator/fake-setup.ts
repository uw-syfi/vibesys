/**
 * The fake host's setup phase: a run in SETUP with a scripted lead, whose six checks tick from Pending
 * to Passed one step for each message the person sends it (no clock, so a fixture page and a test both
 * read the same every time). The host's own work is written once here: what the view says at each step,
 * the refusal of a Start whose checks the host does not read as passed, a read that failed, and the swap
 * of the run into optimizing. The app derives none of it; it reads `RunSummary.setup`.
 *
 * Flavors, each one story:
 * - `ticks`: every step passes, and Start swaps the run.
 * - `refused`: the host reads the number as unstable when Start is pressed (it refuses naming that check),
 *   and passes it after one more message.
 * - `unreadable`: the host cannot read the task at one step (the facts stay, with the reason), and can
 *   again at the next.
 * - `pending`: Start is taken while the lead is in a turn, and the swap waits for `restLead`.
 */
import {create} from '@bufbuild/protobuf';
import {DurationSchema, timestampFromMs} from '@bufbuild/protobuf/wkt';
import {
  AcceleratorModel,
  AcceleratorVendor,
  type Agent,
  AgentRole,
  AgentSchema,
  AgentStatus,
  AgentStopStage,
  type BeginSetup,
  CapabilitiesSchema,
  type CompleteSetup,
  Delivery,
  Harness,
  MessageSchema,
  type Receipt,
  ReceiptSchema,
  Refusal,
  type Run,
  RunPhase,
  RunSchema,
  RunStatus,
  SetupCheckId,
  SetupCheckSchema,
  SetupCheckState,
  type SetupView,
  SetupViewSchema,
  TaskCardSchema,
  TaskDomain,
  TaskObjectiveDirection,
  TurnSchema,
  TurnStatus,
  TurnTrigger,
} from '@vibesys/orchestrator-proto';
import type {OrchestratorState} from '../../state/orchestrator/state.js';
import {itemOf} from './builders.js';
import {type Body, ChangeLog} from './log.js';
import type {Scenario} from './scenario.js';
import {START_MS} from './scenario.js';

export type SetupFlavor = 'ticks' | 'refused' | 'unreadable' | 'pending';

export const SETUP_FLAVORS: readonly SetupFlavor[] = ['ticks', 'refused', 'unreadable', 'pending'];

/** What the host does for the setup world: the facts it commits, the view it serves, the state it holds. */
export interface Ledger {
  record(atMs: number, body: Body): void;
  state(): OrchestratorState;
  publishSetup(runId: string, view: SetupView): void;
}

/** The ways the fake daemon's commands reach the setup world. */
export interface SetupHost {
  begin(commandId: string, request: BeginSetup, atMs: number): Receipt;
  complete(commandId: string, request: CompleteSetup, atMs: number): Receipt;
  /** The person wrote to `agent`: the lead answers, and the checks move on. */
  afterMessage(agent: Agent, text: string, atMs: number): void;
}

const REPOSITORY = 'decode-service';
const TASK = 'mpmc';
const FIRST_QUESTION =
  'What should get faster, and how do you run it today? A command or a script is enough.';

interface Step {
  readonly tools: readonly string[];
  readonly say: string;
}

/** What the lead does for the first five messages: the work behind each tick. */
const STEPS: readonly Step[] = [
  {
    tools: ['read Cargo.toml, benches/, src/lib.rs'],
    say: 'Found benches/throughput.rs. I will add a 4 producer, 4 consumer case. What must stay correct?',
  },
  {
    tools: ['write .vibesys/tasks/mpmc/objectives.toml', 'write .vibesys/tasks/mpmc/check.sh'],
    say: 'The check and the benchmark are written. Running the real jobs now.',
  },
  {
    tools: ['vibesys-cli check mpmc'],
    say: 'The base code passes the check.',
  },
  {
    tools: ['vibesys-cli check mpmc --commit planted-bug'],
    say: 'A broken version fails the check, as it should.',
  },
  {
    tools: ['vibesys-cli measure mpmc --samples 5'],
    say: 'The number is stable. The task is ready; start the run when you are.',
  },
];

/** Each check: the step it passes at, the step it starts running from (if it runs at all), and its detail once passed. */
const CHECKS = [
  {id: SetupCheckId.OBJECTIVE, passAt: 1, detail: 'ops/s, higher is better'},
  {id: SetupCheckId.CORRECTNESS_CHECK, passAt: 2, detail: 'check.sh'},
  {id: SetupCheckId.BENCHMARK, passAt: 2, detail: 'cargo bench, median of 5'},
  {id: SetupCheckId.BASE_PASSES, passAt: 3, runsFrom: 2, detail: 'passed on the base commit'},
  {id: SetupCheckId.BROKEN_FAILS, passAt: 4, runsFrom: 3, detail: 'rejected on a planted bug'},
  {id: SetupCheckId.NUMBER_STABLE, passAt: 5, runsFrom: 4, detail: 'spread 1.8% over 5 samples'},
] as const;

const ORDER: readonly SetupCheckId[] = CHECKS.map(check => check.id);

const {PENDING, RUNNING, PASSED, FAILED} = SetupCheckState;

/** Each check's state and detail after step `n` (0 is before the person says anything). */
function checkStates(n: number): readonly (readonly [SetupCheckState, string])[] {
  return CHECKS.map((check): readonly [SetupCheckState, string] => {
    if (n >= check.passAt) return [PASSED, check.detail];
    const runsFrom = 'runsFrom' in check ? check.runsFrom : undefined;
    return [runsFrom !== undefined && n >= runsFrom ? RUNNING : PENDING, ''];
  });
}

const card = () =>
  create(TaskCardSchema, {
    task: TASK,
    domain: TaskDomain.DATA_STRUCTURES,
    hardware: {vendor: AcceleratorVendor.CPU, model: AcceleratorModel.UNSPECIFIED, count: 1},
    hardwareName: 'CPU',
    workloadName: 'Ring buffer',
    objectives: [{name: 'ops', direction: TaskObjectiveDirection.MAX, unit: 'ops/s'}],
  });

/** The six checks after step `n`; `unstable`: the host read a wide spread on the number. */
function checkRows(n: number, unstable: boolean) {
  const states = checkStates(n);
  return ORDER.map((id, index) => {
    const [state, detail] = states[index] ?? [PENDING, ''];
    const wide = unstable && id === SetupCheckId.NUMBER_STABLE;
    return create(SetupCheckSchema, {
      id,
      state: wide ? FAILED : state,
      detail: wide ? 'spread 9.1% over the 5% floor' : detail,
    });
  });
}

/** The view the host serves after step `n`, `unstable` when its read of the number found a wide spread. */
function setupViewAt(
  n: number,
  options: {readonly failure?: string; readonly unstable?: boolean} = {},
): SetupView {
  const checks = checkRows(n, options.unstable === true);
  return create(SetupViewSchema, {
    task: n >= 1 ? TASK : '',
    draft:
      n >= 1
        ? {case: 'card', value: card()}
        : {case: 'unreadable', value: {detail: 'no task under .vibesys/tasks yet'}},
    checks,
    ...(n >= 5 && options.unstable !== true
      ? {baselineValue: 21.4e6, baselineUnit: 'ops/s', baselineSpreadPercent: 1.8}
      : {}),
    spentUsd: 0.12 * n,
    elapsed: create(DurationSchema, {seconds: BigInt(75 * n)}),
    readFailure: options.failure ?? '',
  });
}

const leadId = (runId: string): string => `${runId}-lead`;

/** The facts a begun setup commits: the run in SETUP, its lead, the brief, and the lead's first question. */
function beginBodies(
  runId: string,
  repository: string,
  startMs: number,
  harness: Harness,
  model: string,
) {
  const run = (status: RunStatus): Run =>
    create(RunSchema, {
      id: runId,
      repository,
      status,
      phase: RunPhase.SETUP,
      leadAgentId: leadId(runId),
      createdAt: timestampFromMs(startMs),
    });
  const agent = (status: AgentStatus): Agent =>
    create(AgentSchema, {
      id: leadId(runId),
      runId,
      role: AgentRole.LEAD,
      harness,
      model,
      reasoningEffort: 'high',
      stopStage: AgentStopStage.NONE,
      session: 1,
      status,
      startedAt: timestampFromMs(startMs),
      capabilities: create(CapabilitiesSchema, {steers: true, resumes: true}),
    });
  const brief = create(MessageSchema, {
    id: `${runId}-brief`,
    runId,
    toAgentId: leadId(runId),
    delivery: Delivery.CONSUMED,
    at: timestampFromMs(startMs + 1000),
    body: {case: 'brief', value: 'Set up a task for this repository.'},
  });
  const turn = create(TurnSchema, {
    id: `${runId}-turn-1`,
    agentId: leadId(runId),
    ordinal: 1,
    trigger: TurnTrigger.BRIEF,
    triggerMessageId: brief.id,
    status: TurnStatus.COMPLETED,
    startedAt: timestampFromMs(startMs + 1000),
    endedAt: timestampFromMs(startMs + 6000),
  });
  const items = [
    itemOf({
      id: `${runId}-item-1`,
      turnId: turn.id,
      agentId: leadId(runId),
      ordinal: 1,
      atMs: startMs + 2000,
      kind: 'prompt',
      text: 'Set up a task for this repository.',
    }),
    itemOf({
      id: `${runId}-item-2`,
      turnId: turn.id,
      agentId: leadId(runId),
      ordinal: 2,
      atMs: startMs + 5000,
      kind: 'assistant',
      text: FIRST_QUESTION,
    }),
  ];
  return [
    {case: 'run', value: run(RunStatus.RUNNING)},
    {case: 'agent', value: agent(AgentStatus.IDLE)},
    {case: 'message', value: brief},
    {case: 'turn', value: turn},
    ...items.map(value => ({case: 'item', value}) as const),
  ] satisfies Body[];
}

/** A scenario with one run in its setup phase, before the person has said anything. */
export function setupScenario(runId = 'run-setup-1'): Scenario {
  const log = new ChangeLog(runId);
  for (const body of beginBodies(runId, REPOSITORY, START_MS, Harness.CLAUDE_CODE, 'model-a'))
    log.push(START_MS, body);
  return {runId, changes: log.changes, plans: [], deltas: [], endMs: START_MS + 6000};
}

/** The setup of one run: how far its script has gone, and what Start has met. */
interface Progress {
  step: number;
  refusedOnce: boolean;
  /** Start was taken mid-turn and waits for the lead to rest. */
  waiting: CompleteSetup | undefined;
  items: number;
  turns: number;
}

/**
 * The fake host's setup world over a daemon's ledger. It serves the view of each run it holds, answers
 * BeginSetup and CompleteSetup, and has the lead answer each message the person sends it.
 */
export class FakeSetup implements SetupHost {
  readonly #progress = new Map<string, Progress>();
  #begun = 0;

  constructor(
    readonly ledger: Ledger,
    readonly flavor: SetupFlavor,
  ) {}

  /** Serve the first view of a run already in the ledger (the scenario's own). */
  adopt(runId: string): void {
    this.#progress.set(runId, {
      step: 0,
      refusedOnce: false,
      waiting: undefined,
      items: 2,
      turns: 1,
    });
    this.ledger.publishSetup(runId, setupViewAt(0));
  }

  #view(runId: string): SetupView {
    const progress = this.#progress.get(runId);
    const step = progress?.step ?? 0;
    return setupViewAt(step, {
      ...(this.flavor === 'unreadable' && step === 3
        ? {failure: 'The task could not be read just now.'}
        : {}),
      ...(progress?.refusedOnce === true && step === 4 && this.flavor === 'refused'
        ? {unstable: true}
        : {}),
    });
  }

  begin(commandId: string, request: BeginSetup, atMs: number): Receipt {
    this.#begun += 1;
    const runId = `run-setup-begun-${this.#begun}`;
    const model = request.lead?.model || 'model-a';
    for (const body of beginBodies(runId, request.repository, atMs, Harness.CLAUDE_CODE, model))
      this.ledger.record(atMs, body);
    this.adopt(runId);
    return this.#receipt({commandId, runId}, atMs);
  }

  #receipt(
    fields: {commandId: string; runId: string; refusal?: Refusal; unmet?: SetupCheckId[]},
    atMs: number,
  ): Receipt {
    const receipt = create(ReceiptSchema, {
      commandId: fields.commandId,
      runId: fields.runId,
      refusal: fields.refusal ?? Refusal.UNSPECIFIED,
      unmetChecks: fields.unmet ?? [],
    });
    this.ledger.record(atMs, {case: 'receipt', value: receipt});
    return receipt;
  }

  complete(commandId: string, request: CompleteSetup, atMs: number): Receipt {
    const run = this.ledger.state().runs[request.runId];
    const progress = this.#progress.get(request.runId);
    if (run === undefined || progress === undefined)
      return this.#receipt({commandId, runId: request.runId, refusal: Refusal.RUN_NOT_FOUND}, atMs);
    if (run.phase !== RunPhase.SETUP)
      return this.#receipt({commandId, runId: request.runId, refusal: Refusal.NOT_IN_SETUP}, atMs);
    const unmet = this.#unmet(progress);
    if (unmet.length > 0) {
      progress.refusedOnce = true;
      progress.step = Math.min(progress.step, 4);
      this.ledger.publishSetup(request.runId, this.#view(request.runId));
      return this.#receipt(
        {commandId, runId: request.runId, refusal: Refusal.SETUP_INCOMPLETE, unmet},
        atMs,
      );
    }
    if (this.flavor === 'pending' && progress.waiting === undefined) {
      progress.waiting = request;
    } else {
      this.#swap(request, atMs);
    }
    return this.#receipt({commandId, runId: request.runId}, atMs);
  }

  /** The checks the host does not read as passed when Start arrives. */
  #unmet(progress: Progress): SetupCheckId[] {
    if (progress.step < 5)
      return ORDER.filter((_, index) => checkStates(progress.step)[index]?.[0] !== PASSED);
    return this.flavor === 'refused' && !progress.refusedOnce ? [SetupCheckId.NUMBER_STABLE] : [];
  }

  /** The run leaves setup: its phase, its task and card, and the point the limits count from. */
  #swap(request: CompleteSetup, atMs: number): void {
    const run = this.ledger.state().runs[request.runId];
    if (run === undefined) return;
    this.ledger.record(atMs, {
      case: 'run',
      value: create(RunSchema, {
        ...run,
        phase: RunPhase.OPTIMIZE,
        task: request.task,
        taskCard: card(),
        fanoutLimit: request.fanoutLimit,
        optimizeStartedAt: timestampFromMs(atMs),
      }),
    });
  }

  /** The lead finished its turn: a Start that waited for it now swaps the run. */
  restLead(runId: string, atMs: number): void {
    const progress = this.#progress.get(runId);
    const waiting = progress?.waiting;
    if (progress === undefined || waiting === undefined) return;
    progress.waiting = undefined;
    this.#swap(waiting, atMs);
  }

  afterMessage(agent: Agent, text: string, atMs: number): void {
    const progress = this.#progress.get(agent.runId);
    if (progress === undefined || progress.step >= 5) return;
    const state = this.ledger.state();
    if (state.runs[agent.runId]?.phase !== RunPhase.SETUP) return;
    progress.step += 1;
    const step = STEPS[progress.step - 1];
    if (step === undefined) return;
    this.#reply(agent, progress, text, step, atMs);
    this.ledger.publishSetup(agent.runId, this.#view(agent.runId));
  }

  /** The lead's turn for one message: what it was asked, the work it did, and what it said. */
  #reply(agent: Agent, progress: Progress, text: string, step: Step, atMs: number): void {
    progress.turns += 1;
    const turnId = `${agent.runId}-turn-${progress.turns}`;
    const message = Object.values(this.ledger.state().messages)
      .filter(each => each.toAgentId === agent.id && each.fromAgentId === '')
      .at(-1);
    if (message !== undefined)
      this.ledger.record(atMs, {
        case: 'message',
        value: create(MessageSchema, {...message, delivery: Delivery.CONSUMED}),
      });
    this.ledger.record(atMs, {
      case: 'turn',
      value: create(TurnSchema, {
        id: turnId,
        agentId: agent.id,
        ordinal: progress.turns,
        trigger: TurnTrigger.OPERATOR,
        triggerMessageId: message?.id ?? '',
        status: TurnStatus.COMPLETED,
        startedAt: timestampFromMs(atMs),
        endedAt: timestampFromMs(atMs + 4000),
      }),
    });
    const lines: {kind: 'prompt' | 'tool' | 'assistant'; text: string}[] = [
      {kind: 'prompt', text},
      ...step.tools.map(tool => ({kind: 'tool' as const, text: tool})),
      {kind: 'assistant', text: step.say},
    ];
    lines.forEach((line, index) => {
      progress.items += 1;
      this.ledger.record(atMs + 500 * (index + 1), {
        case: 'item',
        value: itemOf({
          id: `${agent.runId}-item-${progress.items}`,
          turnId,
          agentId: agent.id,
          ordinal: progress.items,
          atMs: atMs + 500 * (index + 1),
          kind: line.kind,
          text: line.text,
          ...(line.kind === 'tool' ? {output: 'ok', exit: 0} : {}),
        }),
      });
    });
  }
}
