import {type CliRenderer, TextRenderable} from '@opentui/core';
import {
  type ActiveAgentExecution,
  type ExecutionStatus,
  executionStatusFor,
} from '@vibesys/core-state';
import type {SessionState} from '../session-model.js';
import {visibleActiveExecutions} from '../session-model.js';
import {agentRuntimeLabel} from './agent-runtime-label.js';
import type {Theme} from './theme.js';

/** The one braille spinner every in-flight indicator animates. */
export const SPINNER_FRAMES = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏'];
export const SPINNER_INTERVAL_MS = 120;
/** Status for executions visible in the selected agent conversation. */
export class ActivityBarView {
  readonly output: TextRenderable;
  #executions: VisibleExecution[] = [];
  #frame = 0;
  #timer: ReturnType<typeof setInterval> | null = null;

  constructor(renderer: CliRenderer, theme: Theme, id = 'activity-bar') {
    this.output = new TextRenderable(renderer, {
      id,
      height: 1,
      width: '100%',
      wrapMode: 'none',
      truncate: true,
      fg: theme.textMuted,
      content: '',
      visible: false,
    });
  }

  render(state: SessionState, visible = true): void {
    this.#executions = visible
      ? visibleActiveExecutions(state).map(execution => ({
          execution,
          status: executionStatusFor(state.core.executionStatuses, execution),
        }))
      : [];
    this.output.visible = this.#executions.length > 0;
    this.#refresh();
    this.#syncTimer();
  }

  applyTheme(theme: Theme): void {
    this.output.fg = theme.textMuted;
  }

  destroy(): void {
    if (this.#timer !== null) clearInterval(this.#timer);
    this.#timer = null;
  }

  #syncTimer(): void {
    if (this.#executions.length === 0) {
      if (this.#timer !== null) clearInterval(this.#timer);
      this.#timer = null;
      return;
    }
    if (this.#timer !== null) return;
    this.#timer = setInterval(() => {
      this.#frame = (this.#frame + 1) % SPINNER_FRAMES.length;
      this.#refresh();
    }, SPINNER_INTERVAL_MS);
  }

  #refresh(): void {
    const spinner = SPINNER_FRAMES[this.#frame] ?? SPINNER_FRAMES[0];
    const nowMs = Date.now();
    if (this.#executions.length === 1) {
      const visible = this.#executions[0];
      if (visible === undefined) return;
      const {execution, status} = visible;
      this.output.content = `${spinner} ${activityLine(execution, status, nowMs)}`;
      return;
    }
    const summaries = this.#executions
      .slice(0, 3)
      .map(
        ({execution, status}) =>
          `${status?.agentLabel || roleLabel(execution.agentKind)}: ${activitySummary(execution, status)}${statusSuffix(status)}${runtimeSuffix(execution)}`,
      )
      .join(' · ');
    const remainder = this.#executions.length > 3 ? ` · +${this.#executions.length - 3} more` : '';
    this.output.content = `${spinner} ${this.#executions.length} agents active · ${summaries}${remainder}`;
  }
}

interface VisibleExecution {
  execution: ActiveAgentExecution;
  status: ExecutionStatus | undefined;
}

export function activitySummary(
  _execution: ActiveAgentExecution,
  status?: ExecutionStatus,
): string {
  return status?.progress || 'Working';
}

export function runtimeSuffix(execution: ActiveAgentExecution): string {
  const label = agentRuntimeLabel(execution.provider, execution.model);
  return label === null ? '' : ` · ${label}`;
}

/** Formats the selected active execution's complete status line. */
export function activityLine(
  execution: ActiveAgentExecution,
  status: ExecutionStatus | undefined,
  nowMs: number,
): string {
  const label = status?.agentLabel || roleLabel(execution.agentKind);
  const timing =
    status?.elapsedSeconds === null || status?.elapsedSeconds === undefined
      ? elapsed(execution.startedAt, nowMs)
      : elapsedSeconds(status.elapsedSeconds);
  return `${label} · ${activitySummary(execution, status)}${runtimeSuffix(execution)} · ${timing}${statusSuffix(status)}`;
}

function roleLabel(role: string): string {
  if (role === '') return 'Agent';
  return role.charAt(0).toUpperCase() + role.slice(1).replaceAll('_', ' ');
}

function elapsed(startedAt: string, nowMs: number): string {
  const milliseconds = nowMs - Date.parse(startedAt);
  const seconds = Number.isFinite(milliseconds) ? Math.max(0, Math.floor(milliseconds / 1000)) : 0;
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m ${seconds % 60}s`;
}

function elapsedSeconds(value: number): string {
  const seconds = Math.max(0, Math.floor(value));
  if (seconds < 60) return `${seconds}s`;
  return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
}

export function statusSuffix(status: ExecutionStatus | undefined): string {
  if (status?.inputTokens === null || status?.inputTokens === undefined) return '';
  const used = formatTokenCount(status.inputTokens);
  const window = status.contextWindow;
  if (window === null || window <= 0) return ` · ${used} tokens`;
  const percent = Math.floor((status.inputTokens / window) * 100);
  const pressure = percent >= 95 ? 'critical ' : percent >= 80 ? 'high ' : '';
  return ` · ${used}/${formatTokenCount(window)} context ${pressure}${percent}%`;
}

function formatTokenCount(value: number): string {
  if (value < 1_000) return String(value);
  if (value < 1_000_000) return `${Math.floor(value / 1_000)}k`;
  return `${(value / 1_000_000).toFixed(1)}M`;
}
