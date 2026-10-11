/**
 * Exact-run resume policy shared by the welcome view and the main process.
 *
 * A control for one historical run must carry that run's identity. Missing or malformed identity
 * is unavailable, never an implicit request for the project's latest run.
 */
import {RUN_ID} from './ids.js';

declare const RESUME_RUN_ID: unique symbol;

/** A non-empty run id that is safe to pass as the value after `--resume`. */
export type ResumeRunId = string & {readonly [RESUME_RUN_ID]: true};

export type ResumeDecision =
  | {readonly kind: 'available'; readonly runId: ResumeRunId}
  | {readonly kind: 'unavailable'; readonly reason: string};

export const RUN_ID_UNAVAILABLE = 'Run ID unavailable; this run cannot be resumed safely.';

export class ResumeRequestError extends Error {
  override name = 'ResumeRequestError';
}

/** Validate identity received at the preload/main IPC boundary. */
export function exactResumeRunId(value: unknown): ResumeRunId {
  if (typeof value !== 'string' || !RUN_ID.test(value)) {
    throw new ResumeRequestError(
      'a run id is 1-128 lowercase letters, digits, ".", "_", or "-", starting with a letter or digit',
    );
  }
  return value as ResumeRunId;
}

/** The one decision both run-specific Resume surfaces render. */
export function resumeDecision(runId: string | null): ResumeDecision {
  if (runId === null) return {kind: 'unavailable', reason: RUN_ID_UNAVAILABLE};
  try {
    return {kind: 'available', runId: exactResumeRunId(runId)};
  } catch {
    return {kind: 'unavailable', reason: RUN_ID_UNAVAILABLE};
  }
}

/** Exact resume arguments. The branded input makes a bare `--resume` unrepresentable. */
export function resumeArguments(runId: ResumeRunId): readonly ['--resume', ResumeRunId] {
  return ['--resume', runId];
}
