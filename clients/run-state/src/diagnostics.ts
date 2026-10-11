/**
 * Diagnostics in the run fold: the protocol's diagnostics and the fallbacks read from failure
 * events, merged by identity and severity.
 */
import type {Diagnostic, RunEvent} from '@vibesys/contracts';
import {type CoreDiagnostic, type CoreState, cloneCoreStateWith} from './state.js';
import {configurationFailureContent} from './transcript-fold.js';

/** Returns the one diagnostic added or updated by a reducer transition. */
export function latestDiagnosticChange(
  previous: CoreState,
  current: CoreState,
): CoreDiagnostic | null {
  if (previous.diagnostics === current.diagnostics) return null;
  return (
    current.diagnostics.find((diagnostic, index) => diagnostic !== previous.diagnostics[index]) ??
    null
  );
}

export function applyDiagnosticEvent(state: CoreState, event: RunEvent): CoreState {
  const diagnostic = diagnosticFromEvent(event);
  if (diagnostic === null) return state;
  return cloneCoreStateWith(state, {
    diagnostics: upsertDiagnostic(state.diagnostics, diagnostic),
  });
}

/** Merges `incoming` into the diagnostic it identifies, else appends it. */
export function upsertDiagnostic(
  diagnostics: readonly CoreDiagnostic[],
  incoming: CoreDiagnostic,
): CoreDiagnostic[] {
  const existingIndex = diagnostics.findIndex(existing =>
    incoming.id !== null
      ? existing.id === incoming.id
      : existing.id === null &&
        incoming.invocationId !== null &&
        existing.invocationId === incoming.invocationId,
  );
  if (existingIndex === -1) return [...diagnostics, incoming];
  return diagnostics.map((existing, index) =>
    index === existingIndex ? mergeDiagnostic(existing, incoming) : existing,
  );
}

function mergeDiagnostic(existing: CoreDiagnostic, incoming: CoreDiagnostic): CoreDiagnostic {
  return {
    ...existing,
    ...incoming,
    code: incoming.code ?? existing.code,
    detail: incoming.detail ?? existing.detail,
    hint: incoming.hint ?? existing.hint,
    severity:
      diagnosticSeverityRank(incoming.severity) > diagnosticSeverityRank(existing.severity)
        ? incoming.severity
        : existing.severity,
    // A recorded run_interrupted carries the same diagnostic id as the
    // run_failed that follows it for the same boundary (the backend's coarse
    // RunStatus has no `interrupted` member, so it also reports the generic
    // failure). Both diagnostics merge by that shared id, and `run_interruption`
    // must survive the merge regardless of which side computed it, the same way
    // `terminate` keeps the run's own status from being downgraded by the event
    // that follows it.
    failureKind:
      existing.failureKind === 'run_interruption' || incoming.failureKind === 'run_interruption'
        ? 'run_interruption'
        : incoming.failureKind,
    source: incoming.source ?? existing.source,
    agentKind: incoming.agentKind ?? existing.agentKind,
    roundLabel: incoming.roundLabel ?? existing.roundLabel,
    invocationId: incoming.invocationId ?? existing.invocationId,
    sequence: Math.max(existing.sequence, incoming.sequence),
  };
}

function diagnosticSeverityRank(severity: CoreDiagnostic['severity']): number {
  if (severity === 'fatal') return 2;
  if (severity === 'error') return 1;
  return 0;
}

function diagnosticFromEvent(event: RunEvent): CoreDiagnostic | null {
  const diagnostic = event.diagnostic;
  if (diagnostic !== null && diagnostic !== undefined) {
    return fromProtocolDiagnostic(event, diagnostic);
  }
  const fallback = fallbackDiagnosticFromEvent(event);
  return fallback === null
    ? null
    : fallbackDiagnostic(event, fallback.summary, fallback.scope, fallback.severity, fallback.code);
}

interface FallbackDiagnostic {
  summary: string;
  scope: Diagnostic['scope'];
  severity: CoreDiagnostic['severity'];
  code?: string | null;
}

/** Classifies legacy failure envelopes that predate structured diagnostics. */
function fallbackDiagnosticFromEvent(event: RunEvent): FallbackDiagnostic | null {
  const data = event.data;
  if (data?.kind === 'configuration_failed') {
    return {
      summary: configurationFailureContent(data),
      scope: 'configuration',
      severity: 'fatal',
      code: data.code,
    };
  }
  const invocation = invocationFallbackDiagnostic(event);
  if (invocation !== null) return invocation;
  return runFallbackDiagnostic(event);
}

function invocationFallbackDiagnostic(event: RunEvent): FallbackDiagnostic | null {
  const data = event.data;
  if (
    data?.kind !== 'invocation_finished' ||
    ((data.error === null || data.error === undefined) && event.status !== 'failed')
  ) {
    return null;
  }
  return {
    summary: data.error || event.text || 'Agent invocation failed.',
    scope: 'invocation',
    severity: 'error',
  };
}

function runFallbackDiagnostic(event: RunEvent): FallbackDiagnostic | null {
  if (event.type !== 'run_failed' && event.type !== 'run_interrupted') return null;
  const data = event.data;
  const interruption =
    data?.kind === 'run_interrupted'
      ? `${data.reason}${data.signal === null ? '' : ` (${data.signal})`}`
      : '';
  return {
    summary:
      event.text ||
      interruption ||
      (event.type === 'run_failed' ? 'Run failed.' : 'Run interrupted.'),
    scope: 'run',
    severity: 'fatal',
  };
}

function fromProtocolDiagnostic(event: RunEvent, diagnostic: Diagnostic): CoreDiagnostic {
  return {
    id: diagnostic.id ?? null,
    code: diagnostic.code ?? null,
    failureKind: failureKind(diagnostic.scope, event.type),
    summary: diagnostic.summary,
    detail: diagnostic.detail ?? null,
    hint: diagnostic.hint ?? null,
    severity: diagnostic.severity ?? 'error',
    scope: diagnostic.scope,
    source: diagnostic.source ?? null,
    agentKind: event.agent_kind ?? null,
    roundLabel: event.round_label ?? null,
    invocationId: event.invocation_id ?? null,
    sequence: event.sequence ?? 0,
  };
}

function fallbackDiagnostic(
  event: RunEvent,
  summary: string,
  scope: Diagnostic['scope'],
  severity: CoreDiagnostic['severity'],
  code: string | null = null,
): CoreDiagnostic {
  return {
    id: null,
    code,
    failureKind: failureKind(scope, event.type),
    summary,
    detail: null,
    hint: null,
    severity,
    scope,
    source: null,
    agentKind: event.agent_kind ?? null,
    roundLabel: event.round_label ?? null,
    invocationId: event.invocation_id ?? null,
    sequence: event.sequence ?? 0,
  };
}

function failureKind(
  scope: Diagnostic['scope'],
  eventType: RunEvent['type'],
): CoreDiagnostic['failureKind'] {
  return eventType === 'run_interrupted' ? 'run_interruption' : scope;
}
