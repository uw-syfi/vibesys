/** Reported turn usage is projected by span; absent values stay unknown. */
import {fromBinary} from '@bufbuild/protobuf';
import {timestampDate} from '@bufbuild/protobuf/wkt';
import {validateAgentReply} from '@vibesys/contracts';
import type {UsageRecord} from '@vibesys/contracts/host-helper';
import {AgentClient, type AgentTurnAttrs, AgentTurnAttrsSchema} from '@vibesys/proto';
import {addTokens, type CoreState, initialCoreState, NO_TOKENS, type TokenTally} from '../index.js';
import {ownProjectionInput} from '../publication.js';
import type {TranscriptEntry} from '../transcript-fold.js';
import type {SkeletonContent, StoreView} from './types.js';

function replyEntry(
  spanId: string,
  turn: AgentTurnAttrs,
  seq: bigint,
  content: SkeletonContent,
): TranscriptEntry[] {
  const ref = content.replies?.[spanId]?.filter(item => item.seq <= seq).at(-1);
  const text = turn.replyJson || (ref ? content.text?.[ref.hash] : undefined);
  if (!text) return [];
  const reply = validateAgentReply(JSON.parse(text));
  return [
    {
      id: `reply:${spanId}`,
      kind: 'reply',
      spanId,
      turnId: spanId,
      agentKind: turn.role,
      content: JSON.stringify(reply.value),
      reply: ownProjectionInput(reply),
    },
  ];
}

function count(value: bigint | undefined): number | null {
  if (value === undefined || value > BigInt(Number.MAX_SAFE_INTEGER)) return null;
  return Number(value);
}

/** Who ran the turn: the selected harness and model, and which client executed it. */
function recordedAgent(
  turn: AgentTurnAttrs,
): Pick<UsageRecord, 'client' | 'harness' | 'model' | 'reasoning_effort' | 'harness_version'> {
  return {
    client:
      turn.client === AgentClient.SCRIPTED
        ? 'scripted'
        : turn.client === AgentClient.PROVIDER
          ? 'provider'
          : null,
    harness: turn.provider ?? null,
    model: turn.model ?? null,
    reasoning_effort: turn.reasoningEffort ?? null,
    harness_version: turn.harnessVersion ?? null,
  };
}

export function skeletonCore(view: StoreView, content: SkeletonContent = {}): CoreState {
  const seq = view.seq;
  const bySpan = Object.create(null) as Record<string, TokenTally>;
  const records: UsageRecord[] = [];
  const transcript: TranscriptEntry[] = [];
  for (const span of Object.values(view.spans)) {
    const attrs = span.label?.attrs;
    if (attrs?.typeUrl.split('/').at(-1) !== AgentTurnAttrsSchema.typeName) continue;
    const turn = fromBinary(AgentTurnAttrsSchema, attrs.value);
    transcript.push(...replyEntry(span.id, turn, seq, content));
    const usage = turn.usage;
    const tokens: TokenTally = {
      input: count(usage?.input),
      cacheRead: count(usage?.cacheRead),
      cacheWrite: count(usage?.cacheCreation),
      output: count(usage?.output),
      reasoning: count(usage?.reasoning),
    };
    bySpan[span.id] = tokens;
    records.push({
      run_id: view.epochs.at(-1)?.epoch?.runId ?? null,
      execution_id: null,
      span_id: span.id,
      round_label: null,
      role: turn.role,
      ...recordedAgent(turn),
      started_at: turn.startedAt ? timestampDate(turn.startedAt).toISOString() : null,
      ended_at: turn.endedAt ? timestampDate(turn.endedAt).toISOString() : null,
      cost_usd: usage?.costUsd ?? null,
      duration_ms: count(usage?.durationMs),
      tokens: {
        input: tokens.input,
        cache_read: tokens.cacheRead,
        cache_write: tokens.cacheWrite,
        output: tokens.output,
        reasoning: tokens.reasoning,
      },
    });
  }
  return {
    ...initialCoreState(),
    sequence: Number(seq),
    runId: view.epochs.at(-1)?.epoch?.runId ?? null,
    tokens: {total: Object.values(bySpan).reduce(addTokens, NO_TOKENS), bySpan},
    usageRecords: records,
    transcript,
  };
}
