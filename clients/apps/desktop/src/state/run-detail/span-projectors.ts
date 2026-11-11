import {
  type AgentTurnAttrs,
  AgentTurnAttrsSchema,
  EvidenceKind,
  FrameworkSource,
  type GateAttrs,
  GateAttrsSchema,
  AgentClient as ProtoAgentClient,
  GateStage as ProtoGateStage,
  GateSubject as ProtoGateSubject,
  InputFailureKind as ProtoInputFailureKind,
  SpanFailureKind as ProtoSpanFailureKind,
  WorkstreamFailureKind as ProtoWorkstreamFailureKind,
  type Span,
  SpanStatus,
  validatedAnyUnpack,
  type WorkstreamAttrs,
  WorkstreamAttrsSchema,
} from '@vibesys/proto';
import type {
  AgentLifecycle,
  AgentState,
  CorrectnessGateState,
  FailedCheckProjection,
  GateEvidenceKind,
  GateFrameworkSource,
  GateInputFailureKind,
  GateStage,
  GateSubject,
  InputFailureProjection,
  SpanDeclarationProof,
  SpanFailureKind,
  SpanFailureProjection,
  SpanLinkProjection,
  WorkstreamFailureKind,
  WorkstreamFailureProjection,
  WorkstreamState,
} from './types.js';

const SPAN_STATUSES: Partial<Record<SpanStatus, AgentLifecycle>> = {
  [SpanStatus.RUNNING]: 'running',
  [SpanStatus.WAITING]: 'waiting',
  [SpanStatus.PENDING]: 'pending',
  [SpanStatus.DONE]: 'done',
  [SpanStatus.FAILED]: 'failed',
  [SpanStatus.STOPPED]: 'stopped',
};
const SPAN_FAILURES: Partial<Record<ProtoSpanFailureKind, SpanFailureKind>> = {
  [ProtoSpanFailureKind.OUTCOME]: 'outcome',
  [ProtoSpanFailureKind.INTERRUPTION]: 'interruption',
};
const GATE_STAGES: Partial<Record<ProtoGateStage, GateStage>> = {
  [ProtoGateStage.BUILD]: 'build',
  [ProtoGateStage.ACCURACY]: 'accuracy',
  [ProtoGateStage.BENCHMARK]: 'benchmark',
  [ProtoGateStage.VALIDATION]: 'validation',
  [ProtoGateStage.MEASUREMENT]: 'measurement',
};
const GATE_SUBJECTS: Partial<Record<ProtoGateSubject, GateSubject>> = {
  [ProtoGateSubject.INPUT]: 'input',
  [ProtoGateSubject.CANDIDATE]: 'candidate',
  [ProtoGateSubject.UNATTRIBUTED]: 'unattributed',
};
const FRAMEWORK_SOURCES: Partial<Record<FrameworkSource, GateFrameworkSource>> = {
  [FrameworkSource.GATES]: 'gates',
  [FrameworkSource.GIT_TRACKING]: 'git-tracking',
  [FrameworkSource.LOOP]: 'loop',
  [FrameworkSource.GPU]: 'gpu',
  [FrameworkSource.SKYPILOT]: 'skypilot',
  [FrameworkSource.OTHER]: 'other',
};
const EVIDENCE_KINDS: Partial<Record<EvidenceKind, GateEvidenceKind>> = {
  [EvidenceKind.CORRECTNESS]: 'correctness',
  [EvidenceKind.BENCHMARK]: 'benchmark',
};
const INPUT_FAILURE_KINDS: Partial<Record<ProtoInputFailureKind, GateInputFailureKind>> = {
  [ProtoInputFailureKind.NO_EVIDENCE]: 'no-evidence',
  [ProtoInputFailureKind.READINGS_REFUSED]: 'readings-refused',
  [ProtoInputFailureKind.READINGS_UNDECODABLE]: 'readings-undecodable',
  [ProtoInputFailureKind.ACCURACY_FAILED]: 'accuracy-failed',
  [ProtoInputFailureKind.BENCHMARK_FAILED]: 'benchmark-failed',
  [ProtoInputFailureKind.WAIT_ENDED]: 'wait-ended',
};
const WORKSTREAM_FAILURE_KINDS: Partial<Record<ProtoWorkstreamFailureKind, WorkstreamFailureKind>> =
  {
    [ProtoWorkstreamFailureKind.STOPPED]: 'stopped',
    [ProtoWorkstreamFailureKind.PARENT_UNVERIFIED]: 'parent-unverified',
    [ProtoWorkstreamFailureKind.REFUSED]: 'refused',
    [ProtoWorkstreamFailureKind.ENDED]: 'ended',
    [ProtoWorkstreamFailureKind.OUT_OF_BUDGET]: 'out-of-budget',
    [ProtoWorkstreamFailureKind.TURN_FAILED]: 'turn-failed',
    [ProtoWorkstreamFailureKind.INVALID_REPLY]: 'invalid-reply',
    [ProtoWorkstreamFailureKind.RENDER_FAILED]: 'render-failed',
    [ProtoWorkstreamFailureKind.WAIT_REFUSED]: 'wait-refused',
    [ProtoWorkstreamFailureKind.CHECKPOINT_DECLINED]: 'checkpoint-declined',
    [ProtoWorkstreamFailureKind.NO_CHANGED_CANDIDATE]: 'no-changed-candidate',
    [ProtoWorkstreamFailureKind.IMPLEMENTATION_FAILED]: 'implementation-failed',
    [ProtoWorkstreamFailureKind.REVIEW_REJECTED]: 'review-rejected',
    [ProtoWorkstreamFailureKind.NO_TRUSTED_EVIDENCE]: 'no-trusted-evidence',
    [ProtoWorkstreamFailureKind.EVIDENCE_REFUSED]: 'evidence-refused',
    [ProtoWorkstreamFailureKind.EVIDENCE_UNDECODABLE]: 'evidence-undecodable',
    [ProtoWorkstreamFailureKind.PROFILE_CAPTURE_UNAVAILABLE]: 'profile-capture-unavailable',
    [ProtoWorkstreamFailureKind.TURN_LIMIT]: 'turn-limit',
    [ProtoWorkstreamFailureKind.REPEATED_TURN]: 'repeated-turn',
  };

interface SpanProjectionInput {
  readonly runId: string;
  readonly sequence: bigint;
  readonly epoch: number;
  readonly startProof: SpanDeclarationProof;
  readonly attrsType: string;
  readonly span: Span;
}

export type TypedSpanProjection =
  | {readonly kind: 'agent'; readonly value: AgentState}
  | {readonly kind: 'workstream'; readonly value: WorkstreamState}
  | {readonly kind: 'correctness'; readonly value: CorrectnessGateState}
  | {readonly kind: 'unsupported'}
  | {readonly kind: 'invalid'};

/** Decode one installed attrs contract into a source-neutral projection. */
export function projectTypedSpan(input: SpanProjectionInput): TypedSpanProjection {
  const packed = input.span.label?.attrs;
  if (!packed) return {kind: 'invalid'};
  try {
    switch (input.attrsType) {
      case AgentTurnAttrsSchema.typeName: {
        const attrs = validatedAnyUnpack(packed, AgentTurnAttrsSchema);
        return attrs ? agentProjection(input, attrs) : {kind: 'invalid'};
      }
      case WorkstreamAttrsSchema.typeName: {
        const attrs = validatedAnyUnpack(packed, WorkstreamAttrsSchema);
        return attrs ? workstreamProjection(input, attrs) : {kind: 'invalid'};
      }
      case GateAttrsSchema.typeName: {
        const attrs = validatedAnyUnpack(packed, GateAttrsSchema);
        return attrs ? correctnessProjection(input, attrs) : {kind: 'invalid'};
      }
      default:
        return {kind: 'unsupported'};
    }
  } catch {
    return {kind: 'invalid'};
  }
}

function agentProjection(input: SpanProjectionInput, attrs: AgentTurnAttrs): TypedSpanProjection {
  const lifecycle = SPAN_STATUSES[input.span.status];
  if (!lifecycle) return {kind: 'invalid'};
  return {
    kind: 'agent',
    value: {
      spanId: input.span.id,
      parentSpanId: input.span.parent,
      agentId: attrs.agent,
      role: attrs.role,
      stage: attrs.stage,
      startedBy: attrs.startedBy,
      model: attrs.model,
      provider: attrs.provider,
      reasoningEffort: attrs.reasoningEffort,
      harnessVersion: attrs.harnessVersion,
      client:
        attrs.client === ProtoAgentClient.SCRIPTED
          ? 'scripted'
          : attrs.client === ProtoAgentClient.PROVIDER
            ? 'provider'
            : 'unknown',
      providerSessionIds: attrs.usageDetails.flatMap(detail =>
        detail.providerSessionId === undefined ? [] : [detail.providerSessionId],
      ),
      lifecycle,
      usage: attrs.usage
        ? {
            inputTokens: attrs.usage.input,
            cacheReadTokens: attrs.usage.cacheRead,
            cacheCreationTokens: attrs.usage.cacheCreation,
            outputTokens: attrs.usage.output,
            reasoningTokens: attrs.usage.reasoning,
            costUsd: attrs.usage.costUsd,
            durationMs: attrs.usage.durationMs,
          }
        : undefined,
      source: source(input),
      scope: scope(input),
      startProof: input.startProof,
    },
  };
}

function workstreamProjection(
  input: SpanProjectionInput,
  attrs: WorkstreamAttrs,
): TypedSpanProjection {
  const common = commonSpan(input);
  const failure = workstreamFailure(attrs);
  if (!common || failure === null) return {kind: 'invalid'};
  return {
    kind: 'workstream',
    value: {
      ...common,
      hypothesisId: attrs.hypothesisId,
      parentRevision: attrs.parentRevision,
      baseRound: attrs.baseRound,
      round: attrs.round,
      idea: attrs.idea,
      revision: attrs.revision,
      outcome: attrs.outcome,
      summary: attrs.summary,
      probe: attrs.probe,
      workstreamFailure: failure,
      files: attrs.files.map(file => ({
        path: file.path,
        diffOffset: file.diffOffset,
        diffLength: file.diffLength,
      })),
      attempts: attrs.attempts,
      profileSkipped: attrs.profileSkipped,
      judgeVerdict: attrs.judgeVerdict,
      repeatedError: attrs.repeatedError,
      sequence: attrs.sequence,
      startProof: input.startProof,
    },
  };
}

function correctnessProjection(input: SpanProjectionInput, attrs: GateAttrs): TypedSpanProjection {
  const common = commonSpan(input);
  const stage = GATE_STAGES[attrs.stage];
  const subject = GATE_SUBJECTS[attrs.subject];
  const failedCheck = projectFailedCheck(attrs);
  const inputFailure = projectInputFailure(attrs);
  if (!common || !stage || !subject || failedCheck === null || inputFailure === null) {
    return {kind: 'invalid'};
  }
  return {
    kind: 'correctness',
    value: {
      ...common,
      stage,
      subject,
      recipe: attrs.recipe,
      command: attrs.command,
      reused: attrs.reused,
      failedCheck,
      inputFailure,
      frameworkSource: attrs.source === undefined ? undefined : FRAMEWORK_SOURCES[attrs.source],
      sourceLabel: attrs.sourceLabel,
      stages: attrs.stages,
      startProof: input.startProof,
    },
  };
}

function commonSpan(input: SpanProjectionInput) {
  const label = input.span.label;
  const lifecycle = SPAN_STATUSES[input.span.status];
  if (!label || !lifecycle) return undefined;
  const spanFailure = projectSpanFailure(input.span);
  if (spanFailure === null) return undefined;
  return {
    spanId: input.span.id,
    parentSpanId: input.span.parent,
    title: label.title || input.span.name,
    lifecycle,
    verdict: label.verdict,
    reason: label.reason,
    links: label.links.map(link => ({rel: link.rel, to: link.to}) satisfies SpanLinkProjection),
    startedAt: input.span.startedAt,
    endedAt: input.span.endedAt,
    spanFailure,
    source: source(input),
    scope: scope(input),
  };
}

function projectSpanFailure(span: Span): SpanFailureProjection | undefined | null {
  if (!span.failure) return undefined;
  const kind = SPAN_FAILURES[span.failure.kind];
  return kind ? {kind, message: span.failure.message} : null;
}

function workstreamFailure(attrs: WorkstreamAttrs): WorkstreamFailureProjection | undefined | null {
  if (!attrs.failure) return undefined;
  const kind = WORKSTREAM_FAILURE_KINDS[attrs.failure.kind];
  return kind
    ? {
        kind,
        role: attrs.failure.role,
        turns: attrs.failure.turns,
        limit: attrs.failure.limit,
        text: attrs.failure.text,
      }
    : null;
}

function projectFailedCheck(attrs: GateAttrs): FailedCheckProjection | undefined | null {
  if (!attrs.failed) return undefined;
  const check = EVIDENCE_KINDS[attrs.failed.check];
  return check
    ? {
        check,
        completed: attrs.failed.completed,
        required: attrs.failed.required,
        unit: attrs.failed.unit,
      }
    : null;
}

function projectInputFailure(attrs: GateAttrs): InputFailureProjection | undefined | null {
  if (!attrs.inputFailure) return undefined;
  const kind = INPUT_FAILURE_KINDS[attrs.inputFailure.kind];
  const stage =
    attrs.inputFailure.stage === undefined ? undefined : GATE_STAGES[attrs.inputFailure.stage];
  if (!kind || (attrs.inputFailure.stage !== undefined && !stage)) return null;
  return {
    kind,
    stage,
    attempts: attrs.inputFailure.attempts,
    compared: attrs.inputFailure.compared,
    stageId: attrs.inputFailure.stageId,
  };
}

function source(input: SpanProjectionInput) {
  return {kind: 'run-record' as const, sequence: input.sequence, epoch: input.epoch};
}

function scope(input: SpanProjectionInput) {
  return {kind: 'span' as const, runId: input.runId, spanId: input.span.id};
}
