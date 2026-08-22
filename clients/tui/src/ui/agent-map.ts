import {BoxRenderable, type CliRenderer, TextRenderable} from '@opentui/core';
import {hasActiveAgentTiming} from '../round-timing.js';
import type {AgentPhase, RoundSummary} from '../run-map.js';
import {roundAgentElapsedMs} from '../run-map.js';
import type {SessionController} from '../session-controller.js';
import type {SessionState} from '../session-model.js';
import {scopedRounds, visiblePhases, visibleRoundNumber} from '../session-model.js';
import {elapsedLabel} from './previews.js';
import type {Theme} from './theme.js';

const STATUS_MARKER: Record<AgentPhase['status'], string> = {
  pending: '○',
  active: '◉',
  completed: '✔',
  failed: '×',
};

function statusColor(theme: Theme, status: AgentPhase['status']): string {
  if (status === 'active') return theme.success;
  if (status === 'completed') return theme.info;
  if (status === 'failed') return theme.error;
  return theme.textSubtle;
}

export class AgentMapView {
  readonly output: BoxRenderable;
  #theme: Theme;
  #renderedState: SessionState | null = null;
  #elapsedTimer: ReturnType<typeof setInterval> | null = null;
  #runningRound: {round: RoundSummary; text: TextRenderable} | null = null;

  constructor(
    private readonly renderer: CliRenderer,
    private readonly controller: SessionController,
    theme: Theme,
  ) {
    this.#theme = theme;
    this.output = new BoxRenderable(renderer, {
      id: 'agent-map',
      width: 30,
      height: '100%',
      flexDirection: 'column',
      paddingLeft: 1,
      paddingRight: 1,
      border: true,
      borderStyle: 'rounded',
      borderColor: theme.border,
      title: ' Agents ',
    });
  }

  applyTheme(theme: Theme): void {
    this.#theme = theme;
    this.output.borderColor = theme.border;
    this.#renderedState = null;
  }

  render(state: SessionState): void {
    if (state === this.#renderedState) return;
    this.#renderedState = state;
    this.#clear();
    const phases = visiblePhases(state);
    if (phases.length === 0) {
      this.output.add(
        new TextRenderable(this.renderer, {
          content: 'Waiting for phases…',
          fg: this.#theme.textSubtle,
          width: '100%',
        }),
      );
      return;
    }

    const roundNumber = visibleRoundNumber(state);
    const round =
      roundNumber === null
        ? null
        : (scopedRounds(state).find(item => item.number === roundNumber) ?? null);
    const heading = new TextRenderable(this.renderer, {
      content: headingLabel(roundNumber, round),
      fg: this.#theme.textPrimary,
      width: '100%',
    });
    this.output.add(heading);
    if (round !== null && hasActiveAgentTiming(round)) this.#runningRound = {round, text: heading};
    for (const [index, phase] of phases.entries()) {
      this.output.add(this.#renderPhase(phase, state.selectedAgentKind === phase.kind));
      if (index < phases.length - 1) {
        this.output.add(
          new TextRenderable(this.renderer, {
            content: '        ↓',
            fg: this.#theme.textSubtle,
            width: '100%',
          }),
        );
      }
    }
    this.#syncElapsedTimer();
  }

  destroy(): void {
    this.#stopElapsedTimer();
  }

  #syncElapsedTimer(): void {
    if (this.#runningRound === null || this.#elapsedTimer !== null) return;
    this.#elapsedTimer = setInterval(() => {
      if (this.#runningRound === null) return;
      const {round, text} = this.#runningRound;
      text.content = headingLabel(round.number, round);
    }, 1000);
  }

  #stopElapsedTimer(): void {
    if (this.#elapsedTimer === null) return;
    clearInterval(this.#elapsedTimer);
    this.#elapsedTimer = null;
  }

  #clear(): void {
    this.#runningRound = null;
    this.#stopElapsedTimer();
    for (const child of [...this.output.getChildren()]) {
      this.output.remove(child);
      child.destroyRecursively();
    }
  }

  #renderPhase(phase: AgentPhase, selected: boolean): BoxRenderable {
    const row = new BoxRenderable(this.renderer, {
      id: `agent-${phase.kind}`,
      width: '100%',
      flexDirection: 'column',
      marginTop: 1,
      paddingLeft: 1,
      paddingRight: 1,
      border: selected,
      borderStyle: 'rounded',
      borderColor: selected ? this.#theme.borderFocus : this.#theme.border,
      ...(selected ? {backgroundColor: this.#theme.selectedSurface} : {}),
      onMouseUp: () => this.controller.selectAgent(phase.kind),
    });
    const color = statusColor(this.#theme, phase.status);
    row.add(
      new TextRenderable(this.renderer, {
        content: `${STATUS_MARKER[phase.status]} ${phase.kind}`,
        fg: selected ? this.#theme.textStrong : color,
        width: '100%',
      }),
    );
    row.add(
      new TextRenderable(this.renderer, {
        content: phase.status,
        fg: color,
        width: '100%',
      }),
    );
    if (phase.roundLabel) {
      row.add(
        new TextRenderable(this.renderer, {
          content: phase.roundLabel,
          fg: this.#theme.textMuted,
          width: '100%',
        }),
      );
    }
    return row;
  }
}

function headingLabel(roundNumber: number | null, round: RoundSummary | null): string {
  if (roundNumber === null) return 'Run flow';
  const elapsedMs = round === null ? 0 : roundAgentElapsedMs(round);
  if (elapsedMs <= 0) return `Round ${roundNumber} flow`;
  return `Round ${roundNumber} flow · ${elapsedLabel(elapsedMs)}`;
}