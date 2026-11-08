/** Builders for the facts a scenario is made of, so each is created through its generated schema. */
import {create} from '@bufbuild/protobuf';
import {timestampFromMs} from '@bufbuild/protobuf/wkt';
import {
  AcceleratorModel,
  AcceleratorVendor,
  type Item,
  ItemSchema,
  ItemStatus,
  type Plan,
  PlanSchema,
  type PlanStatus,
  type Run,
  RunSchema,
  type RunStatus,
  ShellCallSchema,
  type TaskCard,
  TaskCardSchema,
  TaskDomain,
  type ToolCall,
  ToolCallSchema,
  type Turn,
  TurnSchema,
  type TurnStatus,
  type TurnTrigger,
  type Usage,
  UsageSchema,
} from '@vibesys/orchestrator-proto';

/** What a fixture task is: its folder name, what it serves or measures, and the hardware it asks for. */
interface FixtureTask {
  readonly task: string;
  readonly model?: string;
  readonly workload?: string;
  /** The accelerator as the task authored it, with its count; absent for a CPU task. */
  readonly gpu?: {readonly name: string; readonly count: number};
}

const FIXTURE_TASKS: readonly FixtureTask[] = [
  {task: 'flint', model: 'Flint 40B A2.5B', gpu: {name: 'AMD MI300X', count: 1}},
  {task: 'ember', model: 'Qwen3 32B', gpu: {name: 'NVIDIA H100', count: 8}},
  {task: 'tokenizer', workload: 'Tokenizer throughput'},
  {task: 'kv-cache', model: 'Llama 3.3 70B', gpu: {name: 'AMD MI300X', count: 4}},
  {task: 'swmr-map', workload: 'Single writer, multiple readers'},
  {task: 'whisper', model: 'Whisper Large v3', gpu: {name: 'NVIDIA H100', count: 1}},
  {task: 'radix-tree', workload: 'Radix tree lookups'},
  {task: 'paged-attention', model: 'Mixtral 8x7B', gpu: {name: 'AMD MI300X', count: 2}},
];

const LONG_MODEL = 'Frontier mixture of experts instruct preview with a very long name';

/**
 * The task a fixture run has and the card the host read of it, by the run's place in its host:
 * a function of `index` alone. `long` swaps the model or workload for one too long for any cell.
 */
export function fixtureTaskOf(
  index: number,
  long = false,
): {readonly task: string; readonly card: TaskCard} {
  const entry = FIXTURE_TASKS[index % FIXTURE_TASKS.length] ?? (FIXTURE_TASKS[0] as FixtureTask);
  const subject = long ? LONG_MODEL : undefined;
  const card = create(TaskCardSchema, {
    task: entry.task,
    domain: entry.model === undefined ? TaskDomain.DATA_STRUCTURES : TaskDomain.LLM_SERVING,
    hardware:
      entry.gpu === undefined
        ? {vendor: AcceleratorVendor.CPU, model: AcceleratorModel.UNSPECIFIED, count: 1}
        : entry.gpu.name.startsWith('NVIDIA')
          ? {vendor: AcceleratorVendor.NVIDIA, model: AcceleratorModel.H100, count: entry.gpu.count}
          : {vendor: AcceleratorVendor.AMD, model: AcceleratorModel.MI300X, count: entry.gpu.count},
    hardwareName: entry.gpu?.name ?? 'CPU',
    modelName: entry.model === undefined ? '' : (subject ?? entry.model),
    workloadName: entry.workload === undefined ? '' : (subject ?? entry.workload),
  });
  return {task: entry.task, card};
}

export function runOf(fields: {
  id: string;
  leadAgentId: string;
  status: RunStatus;
  startMs: number;
  fanout: number;
}): Run {
  return create(RunSchema, {
    id: fields.id,
    repository: 'decode-service',
    task: fixtureTaskOf(Number(fields.id.replace(/\D/g, '') || 0)).task,
    taskCard: fixtureTaskOf(Number(fields.id.replace(/\D/g, '') || 0)).card,
    status: fields.status,
    leadAgentId: fields.leadAgentId,
    fanoutLimit: fields.fanout,
    createdAt: timestampFromMs(fields.startMs),
  });
}

export function turnOf(fields: {
  id: string;
  agentId: string;
  ordinal: number;
  trigger: TurnTrigger;
  triggerMessageId: string;
  startMs: number;
  status: TurnStatus;
}): Turn {
  return create(TurnSchema, {
    id: fields.id,
    agentId: fields.agentId,
    ordinal: fields.ordinal,
    trigger: fields.trigger,
    triggerMessageId: fields.triggerMessageId,
    status: fields.status,
    startedAt: timestampFromMs(fields.startMs),
  });
}

export function itemOf(fields: {
  id: string;
  turnId: string;
  agentId: string;
  ordinal: number;
  atMs: number;
  kind: 'prompt' | 'reasoning' | 'assistant' | 'tool';
  text: string;
  output?: string;
  exit?: number;
  status?: ItemStatus;
  /** What the tool was asked, when it is not a shell command made of `text`. */
  call?: ToolCall['call'];
  /** When the host first saw the item; absent for an item the host recorded before it kept that. */
  startedMs?: number;
}): Item {
  const status = fields.status ?? ItemStatus.FINISHED;
  const tool: ToolCall = create(ToolCallSchema, {
    call: fields.call ?? {case: 'shell', value: create(ShellCallSchema, {command: fields.text})},
    output: fields.output ?? '',
    ...(fields.exit === undefined ? {} : {exitCode: fields.exit}),
  });
  const body =
    fields.kind === 'tool'
      ? ({case: 'tool', value: tool} as const)
      : ({
          case: fields.kind,
          value: {$typeName: 'vibesys.orchestrator.v1.Text', text: fields.text},
        } as const);
  return create(ItemSchema, {
    id: fields.id,
    turnId: fields.turnId,
    agentId: fields.agentId,
    ordinal: fields.ordinal,
    status,
    at: timestampFromMs(fields.atMs),
    ...(fields.startedMs === undefined ? {} : {startedAt: timestampFromMs(fields.startedMs)}),
    body,
  });
}

export function planOf(
  number: number,
  title: string,
  status: PlanStatus | number,
  atMs: number,
): Plan {
  return create(PlanSchema, {
    number,
    title,
    body: `${title}. Measure it against the current decode loop.`,
    status: status as PlanStatus,
    createdAt: timestampFromMs(atMs),
    updatedAt: timestampFromMs(atMs),
  });
}

export function usageOf(fields: {
  input: number;
  output: number;
  cacheRead?: number;
  cacheCreation?: number | undefined;
  reasoning?: number | undefined;
  cost?: number | undefined;
}): Usage {
  return create(UsageSchema, {
    inputTokens: BigInt(fields.input),
    outputTokens: BigInt(fields.output),
    ...(fields.cacheRead === undefined ? {} : {cacheReadTokens: BigInt(fields.cacheRead)}),
    ...(fields.cacheCreation === undefined
      ? {}
      : {cacheCreationTokens: BigInt(fields.cacheCreation)}),
    ...(fields.reasoning === undefined ? {} : {reasoningTokens: BigInt(fields.reasoning)}),
    ...(fields.cost === undefined ? {} : {costUsd: fields.cost}),
  });
}
