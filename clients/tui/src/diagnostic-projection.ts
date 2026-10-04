import {type CoreDiagnostic, type CoreState, latestDiagnosticChange} from '@vibesys/core-state';

/** Whether a rebootstrap replaced one established run with another or unknown log. */
export function coreOwnsDifferentRun(previous: CoreState, next: CoreState): boolean {
  return previous.runId !== null && next.runId !== previous.runId;
}

/**
 * Whether `status` ends a run with a terminal diagnostic the operator did not
 * request and should see. `interrupted` belongs here too: its reason or signal
 * is exactly the detail this banner exists to surface.
 */
export function endedWithBannerableFailure(status: CoreState['status']): boolean {
  return status === 'failed' || status === 'interrupted';
}

/** Whether a changed core diagnostic is new information for the operator. */
export function isNewDiagnostic(previous: CoreState, diagnostic: CoreDiagnostic): boolean {
  if (diagnostic.code !== 'run_identity_mismatch') {
    return !previous.diagnostics.includes(diagnostic);
  }
  // A foreign delivery updates the standing diagnostic's sequence and detail,
  // but does not represent a second fault that should reopen its banner.
  return !previous.diagnostics.some(existing => existing.code === diagnostic.code);
}

/** The first identity mismatch introduced by this transition, if any. */
export function newRunIdentityMismatch(
  previous: CoreState,
  current: CoreState,
): CoreDiagnostic | null {
  const diagnostic = latestDiagnosticChange(previous, current);
  if (diagnostic?.code !== 'run_identity_mismatch') return null;
  return isNewDiagnostic(previous, diagnostic) ? diagnostic : null;
}
