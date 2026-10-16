/**
 * Pure builders from spans to the candidate views: what one span, attempt or candidate shows. The
 * fold (`fold.ts`) decides which spans belong together and when a view must be built again.
 */
import type {Span} from '@vibesys/contracts';
import type {
  Candidate,
  CandidateAttempt,
  CandidateMeasurement,
  CandidateOutcome,
  CandidateStep,
  FailedCheck,
} from './types.js';

type SpanStatus = Span['status'];

const ENDED: ReadonlySet<SpanStatus> = new Set(['done', 'failed', 'stopped']);

export function attributes(span: Span): Record<string, unknown> {
  const text = span.label?.attrs_json;
  if (!text) return {};
  const parsed: unknown = JSON.parse(text);
  return typeof parsed === 'object' && parsed !== null ? (parsed as Record<string, unknown>) : {};
}

export function stringOrNull(value: unknown): string | null {
  return typeof value === 'string' && value !== '' ? value : null;
}

function numberOrNull(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function step(span: Span, kind: string): CandidateStep {
  const role = kind === 'agent_turn' ? stringOrNull(attributes(span)['role']) : null;
  return {
    span: span.id,
    kind,
    title: span.label?.title || span.name,
    role,
    status: span.status,
    startedAt: span.started_at,
    endedAt: span.ended_at ?? null,
  };
}

function measurement(span: Span): CandidateMeasurement {
  const attrs = attributes(span);
  return {
    span: span.id,
    at: span.ended_at ?? span.started_at,
    metricValue: numberOrNull(attrs['metric_value']),
    vsBaseline: numberOrNull(attrs['vs_baseline']),
    verdict: span.label?.verdict ?? null,
    failed: failedCheck(attrs['failed']),
  };
}

function failedCheck(value: unknown): FailedCheck | null {
  if (typeof value !== 'object' || value === null) return null;
  const found = value as Record<string, unknown>;
  const check = stringOrNull(found['check']);
  if (check === null) return null;
  return {
    check,
    completed: numberOrNull(found['completed']),
    required: numberOrNull(found['required']),
    unit: stringOrNull(found['unit']),
  };
}

/** Whether a workstream's attempt was kept, rejected, or still runs. */
function attemptOutcome(span: Span): CandidateOutcome {
  const verdict = span.label?.verdict ?? null;
  if (verdict === 'accepted') return 'kept';
  return verdict !== null || ENDED.has(span.status) ? 'rejected' : 'running';
}

/** The revision a workstream kept; null while it works and when it kept nothing. */
export function keptRevision(span: Span): string | null {
  return attemptOutcome(span) === 'kept' ? stringOrNull(attributes(span)['revision']) : null;
}

/** One attempt: its workstream span and the labelled spans under it, in the order they started. */
export function attempt(span: Span, labelled: readonly Span[]): CandidateAttempt {
  const attrs = attributes(span);
  const outcome = attemptOutcome(span);
  const verdict = span.label?.verdict ?? null;
  const reason =
    outcome !== 'rejected'
      ? null
      : (span.label?.reason ?? span.failure?.kind ?? (verdict === null ? span.status : null));
  const steps = labelled.map(entry => step(entry, entry.label?.kind ?? ''));
  const gates = labelled.filter(entry => entry.label?.kind === 'gate').map(measurement);
  return {
    span: span.id,
    title: span.label?.title || span.name,
    idea: stringOrNull(attrs['idea']),
    round: numberOrNull(attrs['round']),
    outcome,
    reason: reason ?? null,
    revision: keptRevision(span),
    startedAt: span.started_at,
    endedAt: span.ended_at ?? null,
    measurement:
      gates.filter(gate => gate.metricValue !== null || gate.verdict !== null).at(-1) ?? null,
    passed: gates
      .filter(gate => gate.verdict === 'passed')
      .map(gate => gate.metricValue)
      .filter(value => value !== null),
    steps,
  };
}

/** One candidate from its attempts (in the order they started) and the candidate it built on. */
export function candidate(
  id: string,
  hypothesis: string | null,
  parent: string | null,
  attempts: readonly CandidateAttempt[],
): Candidate {
  const last = attempts.at(-1);
  const running = attempts.some(item => item.outcome === 'running');
  const kept = attempts.filter(item => item.outcome === 'kept');
  const outcome: CandidateOutcome = running ? 'running' : kept.length > 0 ? 'kept' : 'rejected';
  const measuredFrom = outcome === 'kept' ? kept : attempts;
  const shown = measuredFrom.filter(item => item.measurement !== null).at(-1);
  return {
    id,
    hypothesis,
    parent,
    title: last?.title ?? id,
    idea:
      attempts
        .map(item => item.idea)
        .filter(idea => idea !== null)
        .at(-1) ?? null,
    round: last?.round ?? null,
    outcome,
    reason: outcome === 'rejected' ? (last?.reason ?? null) : null,
    measurement: shown?.measurement ?? null,
    attempts,
    startedAt: attempts[0]?.startedAt ?? 0,
    endedAt: endOf(attempts, running),
    running: attempts.flatMap(item => item.steps.filter(entry => !ENDED.has(entry.status))),
  };
}

/** When the last of `attempts` ended; null while one runs or its span is still open. */
function endOf(attempts: readonly CandidateAttempt[], running: boolean): number | null {
  const ends = attempts.map(item => item.endedAt);
  if (running || ends.length === 0 || ends.some(end => end === null)) return null;
  return Math.max(...(ends as number[]));
}

/** The best value a passed gate of the candidate measured, the first of equals. */
export function bestPassed(item: Candidate, direction: 'max' | 'min'): number | null {
  let found: number | null = null;
  for (const entry of item.attempts) {
    for (const value of entry.passed) {
      if (found === null || better(value, found, direction)) found = value;
    }
  }
  return found;
}

export function better(value: number, than: number, direction: 'max' | 'min'): boolean {
  return direction === 'min' ? value < than : value > than;
}
