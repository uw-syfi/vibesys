/** Builders for the facts a scenario is made of, so each is created through its generated schema. */
import {create} from '@bufbuild/protobuf';
import {timestampFromMs} from '@bufbuild/protobuf/wkt';
import {
  type Item,
  ItemSchema,
  ItemStatus,
  type Plan,
  PlanSchema,
  type PlanStatus,
  type Run,
  RunSchema,
  type RunStatus,
  type ToolCall,
  ToolCallSchema,
  type Turn,
  TurnSchema,
  type TurnStatus,
  type TurnTrigger,
  type Usage,
  UsageSchema,
} from '@vibesys/orchestrator-proto';

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
    task: 'faster-decode',
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
}): Item {
  const status = fields.status ?? ItemStatus.FINISHED;
  const tool: ToolCall = create(ToolCallSchema, {
    tool: 'shell',
    argsJson: JSON.stringify({command: fields.text}),
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
