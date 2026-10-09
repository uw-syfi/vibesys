import type {ActiveAgentExecution, CoreState} from './core-state.js';
import {type RoundKey, sameRoundKey} from './round-key.js';
import type {AgentPhase} from './run-map.js';

/** What an agent is doing, without exposing a backend label grammar to a UI. */
export interface PhaseDescription {
  activity: string;
  attempt: number | null;
  subject: string | null;
  /** Parsed stage identifier used by semantic selectors such as planning activity. */
  stage: string | null;
}

export type HypothesisPlanningStage = 'pre' | 'profile' | 'plan';

const STAGE_WORDS: Readonly<Record<string, string>> = {
  pre: 'preparing',
  plan: 'planning',
  profiler: 'profiling',
  implementer: 'implementing',
  judge: 'judging',
  mutator: 'mutating',
  'single-agent': 'working',
};

const KIND_WORDS: Readonly<Record<string, string>> = {
  orchestrator: 'planning',
  implementer: 'implementing',
  judge: 'judging',
  profiler: 'profiling',
  perf_eval: 'measuring',
  mutator: 'mutating',
  chat: 'answering',
};

const AGENT_ROUND = /^round-(\d+)(?:-retry-(\d+))?(?:-(.+))?$/;
const EVOLVE_CANDIDATE = /^gen-(\d+)-cand-(\d+)-(.+)$/;
const PLAIN_ISSUE = /^(impl|judge)\s+issue\s+#(\S+)\s+att(\d+)$/;
const PLAIN_PERF = /^perf_eval\s+iter\s+(.+)$/;

interface PhaseContext {
  label: string;
  kind: string;
}

type PhaseParser = (context: PhaseContext) => PhaseDescription | null;

function parseChat({label, kind}: PhaseContext): PhaseDescription | null {
  if (label !== 'experiment chat' && label !== 'experiment-chat' && kind !== 'chat') return null;
  return {activity: 'answering', attempt: null, subject: null, stage: 'chat'};
}

function parseEvolveCandidate({label, kind}: PhaseContext): PhaseDescription | null {
  const match = EVOLVE_CANDIDATE.exec(label);
  if (match === null) return null;
  const [, , candidate, stage] = match;
  return {
    activity: STAGE_WORDS[stage ?? ''] ?? fallbackActivity(stage, kind),
    attempt: null,
    subject: candidate === undefined ? null : `candidate ${candidate}`,
    stage: stage ?? null,
  };
}

function parsePlainIssue({label}: PhaseContext): PhaseDescription | null {
  const match = PLAIN_ISSUE.exec(label);
  if (match === null) return null;
  const [, stage, id, attempt] = match;
  return {
    activity: stage === 'judge' ? 'judging' : 'implementing',
    attempt: attemptOrNull(attempt),
    subject: id === undefined ? null : `issue #${id}`,
    stage: stage ?? null,
  };
}

function parsePlainPerformance({label}: PhaseContext): PhaseDescription | null {
  return PLAIN_PERF.test(label)
    ? {activity: 'measuring', attempt: null, subject: null, stage: 'perf_eval'}
    : null;
}

function parseAgentRound({label, kind}: PhaseContext): PhaseDescription | null {
  const match = AGENT_ROUND.exec(label);
  if (match === null) return null;
  const [, , retry, stage] = match;
  return {
    activity:
      stage === undefined
        ? fallbackActivity(null, kind)
        : (STAGE_WORDS[stage] ?? fallbackActivity(stage, kind)),
    attempt: attemptOrNull(retry),
    subject: null,
    stage: stage ?? null,
  };
}

const PHASE_PARSERS: readonly PhaseParser[] = [
  parseChat,
  parseEvolveCandidate,
  parsePlainIssue,
  parsePlainPerformance,
  parseAgentRound,
];

/** Describes one backend phase through the single core-owned label parser. */
export function describePhase(
  roundLabel: string | null,
  agentKind: string | null,
): PhaseDescription | null {
  const label = roundLabel?.trim() ?? '';
  const kind = agentKind?.trim() ?? '';
  if (label === '' && kind === '') return null;
  const context = {label, kind};
  for (const parse of PHASE_PARSERS) {
    const phase = parse(context);
    if (phase !== null) return phase;
  }
  return {activity: fallbackActivity(null, kind), attempt: null, subject: null, stage: null};
}

/** The hypothesis-planning stage represented by a phase, if any. */
export function planningStageForPhase(
  phase: Pick<AgentPhase, 'kind' | 'roundLabel'>,
): HypothesisPlanningStage | null {
  const stage = describePhase(phase.roundLabel, phase.kind)?.stage;
  if (phase.kind === 'orchestrator' && stage === 'pre') return 'pre';
  if (phase.kind === 'profiler' && stage === 'profiler') return 'profile';
  return phase.kind === 'orchestrator' && stage === 'plan' ? 'plan' : null;
}

/** Human-readable word for a known agent kind, or null when a UI should omit it. */
export function agentKindText(agentKind: string | null): string | null {
  return KIND_WORDS[agentKind?.trim() ?? ''] ?? null;
}

/** Compact phase text shared by every frontend. */
export function phaseText(phase: PhaseDescription | null): string | null {
  if (phase === null) return null;
  const subject = phase.subject === null ? phase.activity : `${phase.activity} ${phase.subject}`;
  return phase.attempt === null ? subject : `${subject} · attempt ${phase.attempt}`;
}

export interface RunFocus {
  executionId: string | null;
  agentKind: string;
  roundKey: RoundKey | null;
  roundLabel: string | null;
  roundNumber: number | null;
  description: PhaseDescription;
}

/**
 * Every currently active execution as a stable frontend focus projection.
 *
 * Checkpoint liveness is authoritative. The run map supplies the richer phase
 * scope and is the fallback for legacy streams without execution checkpoints.
 */
export function activeRunFocus(state: Pick<CoreState, 'activeExecutions' | 'phases'>): RunFocus[] {
  const executions = Object.values(state.activeExecutions).sort(compareExecutions);
  if (executions.length > 0)
    return executions.map(execution => focusForExecution(state, execution));
  return state.phases
    .filter(phase => phase.status === 'active')
    .flatMap(phase => focusFromPhase(phase));
}

function focusForExecution(
  state: Pick<CoreState, 'phases'>,
  execution: ActiveAgentExecution,
): RunFocus {
  const phase = state.phases.find(
    candidate =>
      candidate.executionId === execution.executionId &&
      candidate.status === 'active' &&
      candidate.kind === execution.agentKind &&
      sameRoundKey(candidate.roundKey, execution.roundKey) &&
      candidate.startedAt === execution.startedAt,
  );
  return makeFocus(
    execution.executionId,
    phase?.kind ?? execution.agentKind,
    phase?.roundKey ?? execution.roundKey,
    phase?.roundLabel ?? execution.roundLabel,
    phase?.roundNumber ?? execution.roundNumber,
  );
}

function focusFromPhase(phase: AgentPhase): RunFocus[] {
  const description = describePhase(phase.roundLabel, phase.kind);
  return description === null
    ? []
    : [
        {
          executionId: phase.executionId ?? null,
          agentKind: phase.kind,
          roundKey: phase.roundKey,
          roundLabel: phase.roundLabel,
          roundNumber: phase.roundNumber,
          description,
        },
      ];
}

function makeFocus(
  executionId: string,
  agentKind: string,
  roundKey: RoundKey | null,
  roundLabel: string | null,
  roundNumber: number | null,
): RunFocus {
  return {
    executionId,
    agentKind,
    roundKey,
    roundLabel,
    roundNumber,
    description: describePhase(roundLabel, agentKind) ?? {
      activity: 'working',
      attempt: null,
      subject: null,
      stage: null,
    },
  };
}

function compareExecutions(left: ActiveAgentExecution, right: ActiveAgentExecution): number {
  return (
    left.startedAt.localeCompare(right.startedAt) ||
    left.executionId.localeCompare(right.executionId)
  );
}

function attemptOrNull(value: string | undefined): number | null {
  if (value === undefined) return null;
  const attempt = Number(value);
  return Number.isFinite(attempt) && attempt > 1 ? attempt : null;
}

function fallbackActivity(stage: string | undefined | null, kind: string): string {
  if (stage !== undefined && stage !== null) {
    const known = STAGE_WORDS[stage];
    if (known !== undefined) return known;
  }
  return KIND_WORDS[kind] ?? (kind === '' ? 'working' : kind);
}
