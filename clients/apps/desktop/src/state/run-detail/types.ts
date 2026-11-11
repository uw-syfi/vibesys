export interface RunDetailLimits {
  readonly maxAgents: number;
  readonly maxCorrectnessGates: number;
  readonly maxDescriptorSets: number;
  readonly maxEpochVersions: number;
  readonly maxIssues: number;
  readonly maxMetricPointsPerSeries: number;
  readonly maxMetricSeries: number;
  readonly maxWorkstreams: number;
}

export const DEFAULT_RUN_DETAIL_LIMITS: RunDetailLimits = {
  maxAgents: 512,
  maxCorrectnessGates: 512,
  maxDescriptorSets: 64,
  maxEpochVersions: 128,
  maxIssues: 128,
  maxMetricPointsPerSeries: 512,
  maxMetricSeries: 128,
  maxWorkstreams: 512,
};

export type RunDetailCursor =
  | {readonly kind: 'empty'}
  | {
      readonly kind: 'ready';
      readonly nextSequence: bigint;
      readonly committedSequence: bigint;
      readonly complete: boolean;
      /** Published logs are contiguous; an unheaded phase-1 cursor is only a lower bound. */
      readonly sequencePolicy: 'contiguous' | 'lower-bound';
    }
  | {
      readonly kind: 'refresh-required';
      readonly nextSequence: bigint;
      readonly observedSequence: bigint;
      readonly reason:
        | 'gap'
        | 'history-evicted'
        | 'invalid-order'
        | 'invalid-record'
        | 'invalid-through-sequence';
    };

export type RunDetailFreshness =
  | {readonly kind: 'empty'}
  | {readonly kind: 'current'; readonly throughSequence: bigint}
  | {
      readonly kind: 'catching-up';
      readonly throughSequence: bigint | undefined;
      readonly committedSequence: bigint;
    }
  | {
      readonly kind: 'refresh-required';
      readonly throughSequence: bigint | undefined;
      readonly expectedSequence: bigint;
      readonly observedSequence: bigint;
    };

interface RunLogSource {
  readonly kind: 'run-log';
}

interface RunRecordSource {
  readonly kind: 'run-record';
  readonly sequence: bigint;
  readonly epoch: number;
}

interface RunScope {
  readonly kind: 'run';
  readonly runId: string;
}

interface SpanScope {
  readonly kind: 'span';
  readonly runId: string;
  readonly spanId: string;
}

export type ProjectionReason =
  | 'declaration-unknown'
  | 'descriptor-unknown'
  | 'invalid-typed-fact'
  | 'projection-not-installed'
  | 'state-truncated'
  | 'typed-fact-not-recorded';

export type ProjectionAvailability =
  | {readonly kind: 'available'}
  | {readonly kind: 'partial'; readonly reasons: readonly ProjectionReason[]}
  | {readonly kind: 'unavailable'; readonly reason: ProjectionReason};

export interface ProjectionSet<T> {
  readonly source: RunLogSource;
  readonly scope: RunScope;
  readonly freshness: RunDetailFreshness;
  readonly availability: ProjectionAvailability;
  readonly values: readonly T[];
}

type SpanLifecycle = 'running' | 'waiting' | 'pending' | 'done' | 'failed' | 'stopped';
export type AgentLifecycle = SpanLifecycle;
type AgentClient = 'scripted' | 'provider' | 'unknown';

interface AgentUsageProjection {
  readonly inputTokens: bigint | undefined;
  readonly cacheReadTokens: bigint | undefined;
  readonly cacheCreationTokens: bigint | undefined;
  readonly outputTokens: bigint | undefined;
  readonly reasoningTokens: bigint | undefined;
  readonly costUsd: number | undefined;
  readonly durationMs: bigint | undefined;
}

interface AgentProjection {
  readonly spanId: string;
  readonly parentSpanId: string | undefined;
  readonly agentId: string;
  /** Strategy-selected role. This is intentionally an open string. */
  readonly role: string;
  readonly stage: string;
  readonly startedBy: readonly string[];
  readonly model: string | undefined;
  readonly provider: string | undefined;
  readonly reasoningEffort: string | undefined;
  readonly harnessVersion: string | undefined;
  readonly client: AgentClient;
  readonly providerSessionIds: readonly string[];
  readonly lifecycle: AgentLifecycle;
  readonly usage: AgentUsageProjection | undefined;
  readonly source: RunRecordSource;
  readonly scope: SpanScope;
}

export type MetricDirection = 'max' | 'min' | 'none';
export type MetricRole = 'objective' | 'resource' | 'reference';

interface MetricPointProjection {
  readonly value: number;
  readonly clock: number;
  readonly source: RunRecordSource;
  readonly scope: RunScope | SpanScope;
}

interface MetricSeriesProjection {
  readonly name: string;
  readonly unit: string;
  readonly direction: MetricDirection;
  readonly role: MetricRole;
  readonly source: RunRecordSource;
  readonly scope: RunScope;
  readonly points: readonly MetricPointProjection[];
  readonly omittedPoints: number;
}

export interface ObjectiveProjection {
  readonly series: string;
  readonly direction: Exclude<MetricDirection, 'none'>;
  readonly source: RunRecordSource;
  readonly scope: RunScope;
}

export interface SpanLinkProjection {
  readonly rel: string;
  readonly to: string;
}

export type SpanFailureKind = 'outcome' | 'interruption';

export interface SpanFailureProjection {
  readonly kind: SpanFailureKind;
  readonly message: string;
}

export type WorkstreamFailureKind =
  | 'stopped'
  | 'parent-unverified'
  | 'refused'
  | 'ended'
  | 'out-of-budget'
  | 'turn-failed'
  | 'invalid-reply'
  | 'render-failed'
  | 'wait-refused'
  | 'checkpoint-declined'
  | 'no-changed-candidate'
  | 'implementation-failed'
  | 'review-rejected'
  | 'no-trusted-evidence'
  | 'evidence-refused'
  | 'evidence-undecodable'
  | 'profile-capture-unavailable'
  | 'turn-limit'
  | 'repeated-turn';

export interface WorkstreamFailureProjection {
  readonly kind: WorkstreamFailureKind;
  /** Strategy-selected role; deliberately open. */
  readonly role: string | undefined;
  readonly turns: number | undefined;
  readonly limit: number | undefined;
  readonly text: string;
}

interface WorkstreamFileProjection {
  readonly path: string;
  readonly diffOffset: bigint;
  readonly diffLength: bigint;
}

interface WorkstreamProjection {
  readonly spanId: string;
  readonly parentSpanId: string | undefined;
  readonly title: string;
  readonly lifecycle: SpanLifecycle;
  readonly verdict: string | undefined;
  readonly reason: string | undefined;
  readonly links: readonly SpanLinkProjection[];
  readonly startedAt: number;
  readonly endedAt: number | undefined;
  readonly spanFailure: SpanFailureProjection | undefined;
  readonly hypothesisId: string | undefined;
  readonly parentRevision: string | undefined;
  readonly baseRound: number | undefined;
  readonly round: number | undefined;
  readonly idea: string | undefined;
  readonly revision: string | undefined;
  readonly outcome: string | undefined;
  readonly summary: string | undefined;
  readonly probe: boolean;
  readonly workstreamFailure: WorkstreamFailureProjection | undefined;
  readonly files: readonly WorkstreamFileProjection[];
  readonly attempts: number | undefined;
  readonly profileSkipped: boolean | undefined;
  readonly judgeVerdict: string | undefined;
  readonly repeatedError: boolean;
  readonly sequence: number | undefined;
  readonly source: RunRecordSource;
  readonly scope: SpanScope;
}

export type GateStage = 'build' | 'accuracy' | 'benchmark' | 'validation' | 'measurement';
export type GateSubject = 'input' | 'candidate' | 'unattributed';
export type GateFrameworkSource = 'gates' | 'git-tracking' | 'loop' | 'gpu' | 'skypilot' | 'other';
export type GateEvidenceKind = 'correctness' | 'benchmark';
export type GateInputFailureKind =
  | 'no-evidence'
  | 'readings-refused'
  | 'readings-undecodable'
  | 'accuracy-failed'
  | 'benchmark-failed'
  | 'wait-ended';

export interface FailedCheckProjection {
  readonly check: GateEvidenceKind;
  readonly completed: bigint | undefined;
  readonly required: bigint | undefined;
  readonly unit: string | undefined;
}

export interface InputFailureProjection {
  readonly kind: GateInputFailureKind;
  readonly stage: GateStage | undefined;
  readonly attempts: number;
  readonly compared: boolean;
  readonly stageId: string | undefined;
}

interface CorrectnessGateProjection {
  readonly spanId: string;
  readonly parentSpanId: string | undefined;
  readonly title: string;
  readonly lifecycle: SpanLifecycle;
  readonly verdict: string | undefined;
  readonly reason: string | undefined;
  readonly links: readonly SpanLinkProjection[];
  readonly startedAt: number;
  readonly endedAt: number | undefined;
  readonly spanFailure: SpanFailureProjection | undefined;
  readonly stage: GateStage;
  readonly subject: GateSubject;
  readonly recipe: string | undefined;
  readonly command: string | undefined;
  readonly reused: boolean;
  readonly failedCheck: FailedCheckProjection | undefined;
  readonly inputFailure: InputFailureProjection | undefined;
  /** Framework provenance only; it never implies host or run resource attribution. */
  readonly frameworkSource: GateFrameworkSource | undefined;
  readonly sourceLabel: string | undefined;
  readonly stages: readonly string[];
  readonly source: RunRecordSource;
  readonly scope: SpanScope;
}

export interface UnprojectedSpanProjection {
  readonly spanId: string;
  readonly kind: string;
  readonly attrsType: string;
  readonly declaredAttrsType: string | undefined;
  readonly reason: AttributeIssue['reason'];
  readonly source: RunRecordSource;
  readonly scope: SpanScope;
}

type RunLifecycle =
  | 'starting'
  | 'running'
  | 'held'
  | 'draining'
  | 'parked'
  | 'finished'
  | 'failed'
  | 'stopped'
  | 'interrupted';

export interface LifecycleProjection {
  readonly value: RunLifecycle;
  readonly source: RunRecordSource;
  readonly scope: RunScope;
}

export interface RunDetailProjection {
  readonly runId: string;
  readonly freshness: RunDetailFreshness;
  readonly truncation: Omit<RunDetailTruncation, 'descriptorSetEvictions'>;
  readonly lifecycle: LifecycleProjection | undefined;
  readonly agents: ProjectionSet<AgentProjection>;
  readonly metrics: ProjectionSet<MetricSeriesProjection>;
  readonly objectives: ProjectionSet<ObjectiveProjection>;
  readonly workstreams: ProjectionSet<WorkstreamProjection>;
  readonly correctness: ProjectionSet<CorrectnessGateProjection>;
  readonly unprojectedSpans: ProjectionSet<UnprojectedSpanProjection>;
}

export interface AttributeIssue {
  readonly spanId: string;
  readonly kind: string;
  readonly attrsType: string;
  readonly declaredAttrsType: string | undefined;
  readonly sequence: bigint;
  readonly epoch: number;
  readonly startedSequence: bigint | undefined;
  readonly startedEpoch: number | undefined;
  /** Present only when START was validated; retained so later updates stay attributable. */
  readonly startProof: SpanDeclarationProof | undefined;
  readonly reason:
    | 'undeclared-kind'
    | 'missing-span-start'
    | 'descriptor-unknown'
    | 'descriptor-mismatch'
    | 'invalid-attributes'
    | 'projector-not-installed';
}

interface RunDetailTruncation {
  readonly agentEvictions: number;
  readonly correctnessGateEvictions: number;
  readonly descriptorSetEvictions: number;
  readonly epochEvictions: number;
  readonly issueEvictions: number;
  readonly metricSeriesEvictions: number;
  readonly workstreamEvictions: number;
}

export interface DescriptorKnowledge {
  readonly sha256: string;
  /** Verified encoded FileDescriptorSet, retained for a later hash-addressed keyframe. */
  readonly data: Uint8Array;
  readonly messageTypes: readonly string[];
}

interface SpanKindKnowledge {
  readonly kind: string;
  readonly attrsType: string;
  readonly descriptorKnown: boolean;
}

interface MetricDeclaration {
  readonly name: string;
  readonly unit: string;
  readonly direction: MetricDirection | undefined;
  readonly role: MetricRole | undefined;
}

export interface EpochKnowledge {
  readonly sequence: bigint;
  readonly epoch: number;
  readonly spanKinds: readonly SpanKindKnowledge[];
  readonly metrics: readonly MetricDeclaration[];
  readonly objectives: readonly Omit<ObjectiveProjection, 'source' | 'scope'>[];
}

export interface SpanDeclarationProof {
  readonly startedSequence: bigint;
  readonly startedEpoch: number;
  readonly kind: string;
  readonly attrsType: string;
}

export interface AgentState extends AgentProjection {
  /** Bounded proof resolved at START; latest source provenance remains independent. */
  readonly startProof: SpanDeclarationProof;
}

export interface WorkstreamState extends WorkstreamProjection {
  /** Bounded proof resolved at START; latest source provenance remains independent. */
  readonly startProof: SpanDeclarationProof;
}

export interface CorrectnessGateState extends CorrectnessGateProjection {
  /** Bounded proof resolved at START; latest source provenance remains independent. */
  readonly startProof: SpanDeclarationProof;
}

export interface MetricSeriesState extends MetricSeriesProjection {
  readonly epoch: number;
}

export interface RunDetailState {
  readonly runId: string;
  readonly limits: RunDetailLimits;
  readonly cursor: RunDetailCursor;
  readonly throughSequence: bigint | undefined;
  readonly descriptors: readonly DescriptorKnowledge[];
  readonly epochs: readonly EpochKnowledge[];
  readonly agents: readonly AgentState[];
  readonly workstreams: readonly WorkstreamState[];
  readonly correctnessGates: readonly CorrectnessGateState[];
  readonly metrics: readonly MetricSeriesState[];
  readonly lifecycle: LifecycleProjection | undefined;
  readonly issues: readonly AttributeIssue[];
  readonly undeclaredMetricPoints: number;
  readonly truncation: RunDetailTruncation;
}

export type RunDetailUpdate =
  | {readonly kind: 'applied' | 'duplicate'; readonly state: RunDetailState}
  | {
      readonly kind: 'refresh-required';
      readonly state: RunDetailState;
      readonly reason: Extract<RunDetailCursor, {kind: 'refresh-required'}>['reason'];
    }
  | {
      readonly kind: 'rejected-snapshot';
      readonly state: RunDetailState;
      readonly reason: string;
    };
