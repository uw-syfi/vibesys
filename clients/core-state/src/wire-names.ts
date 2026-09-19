import {
  type AgentExecutionActivityData,
  DiagnosticScope,
  DiagnosticSeverity,
  ExecutionActivityMode,
  RunStatus,
} from '@vibesys/backend-client';

export type AgentExecutionMode = 'thinking' | 'responding' | 'tool' | 'waiting';

export type DiagnosticSeverityName = 'warning' | 'error' | 'fatal';

/** The lowercase scope names core state and its consumers use for `DiagnosticScope`. */
export type DiagnosticScopeName =
  | 'configuration'
  | 'invocation'
  | 'phase'
  | 'run'
  | 'request'
  | 'protocol'
  | 'transport';

/**
 * Run status as core state carries it: every status the backend reports, plus
 * the client-only `connecting` that precedes the first snapshot or event.
 */
export type CoreRunStatus =
  | 'connecting'
  | 'starting'
  | 'running'
  | 'pausing'
  | 'paused'
  | 'stopping'
  | 'stopped'
  | 'completed'
  | 'failed';

/** The core status a wire status maps to; `undefined` for an unspecified one. */
export function coreRunStatus(status: RunStatus): CoreRunStatus | undefined {
  switch (status) {
    case RunStatus.STARTING:
      return 'starting';
    case RunStatus.RUNNING:
      return 'running';
    case RunStatus.PAUSING:
      return 'pausing';
    case RunStatus.PAUSED:
      return 'paused';
    case RunStatus.STOPPING:
      return 'stopping';
    case RunStatus.STOPPED:
      return 'stopped';
    case RunStatus.COMPLETED:
      return 'completed';
    case RunStatus.FAILED:
      return 'failed';
    case RunStatus.UNSPECIFIED:
      return undefined;
    default: {
      const unhandled: never = status;
      return unhandled;
    }
  }
}

function activityModeName(mode: ExecutionActivityMode): AgentExecutionMode {
  switch (mode) {
    case ExecutionActivityMode.THINKING:
      return 'thinking';
    case ExecutionActivityMode.RESPONDING:
      return 'responding';
    case ExecutionActivityMode.TOOL:
      return 'tool';
    case ExecutionActivityMode.WAITING:
    case ExecutionActivityMode.UNSPECIFIED:
      return 'waiting';
    default: {
      const unhandled: never = mode;
      return unhandled;
    }
  }
}

export function activityFrom(activity: AgentExecutionActivityData | undefined): {
  mode: AgentExecutionMode;
  summary: string;
  tool: string | null;
} {
  return {
    mode: activityModeName(activity?.mode ?? ExecutionActivityMode.UNSPECIFIED),
    summary: activity?.summary ?? '',
    tool: activity?.tool ?? null,
  };
}

export function scopeName(scope: DiagnosticScope): DiagnosticScopeName {
  switch (scope) {
    case DiagnosticScope.CONFIGURATION:
      return 'configuration';
    case DiagnosticScope.INVOCATION:
      return 'invocation';
    case DiagnosticScope.PHASE:
      return 'phase';
    case DiagnosticScope.REQUEST:
      return 'request';
    case DiagnosticScope.PROTOCOL:
      return 'protocol';
    case DiagnosticScope.TRANSPORT:
      return 'transport';
    case DiagnosticScope.RUN:
    case DiagnosticScope.UNSPECIFIED:
      return 'run';
    default: {
      const unhandled: never = scope;
      return unhandled;
    }
  }
}

export function severityName(severity: DiagnosticSeverity): DiagnosticSeverityName {
  switch (severity) {
    case DiagnosticSeverity.WARNING:
      return 'warning';
    case DiagnosticSeverity.FATAL:
      return 'fatal';
    case DiagnosticSeverity.ERROR:
    case DiagnosticSeverity.UNSPECIFIED:
      return 'error';
    default: {
      const unhandled: never = severity;
      return unhandled;
    }
  }
}

/** Lowercase member name of a generated enum value, for labels (`AgentOutputChannel.TOOL` is `tool`). */
export function enumWord(names: Record<number, string>, value: number): string {
  return (names[value] ?? 'unspecified').toLowerCase();
}
