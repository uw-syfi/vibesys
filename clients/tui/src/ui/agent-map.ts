import {BoxRenderable, type CliRenderer, TextRenderable} from '@opentui/core';
import type {AgentPhase} from '../run-map.js';
import type {SessionState} from '../session-model.js';
import {visiblePhases, visibleRoundNumber} from '../session-model.js';

const STATUS_MARKER: Record<AgentPhase['status'], string> = {
  pending: '○',
  active: '●',
  completed: '✓',
  failed: '×',
};

const STATUS_COLOR: Record<AgentPhase['status'], string> = {
  pending: '#64748b',
  active: '#22c55e',
  completed: '#60a5fa',
  failed: '#f87171',
};

export class AgentMapView {
  readonly output: BoxRenderable;
  #renderedState: SessionState | null = null;

  constructor(private readonly renderer: CliRenderer) {
    this.output = new BoxRenderable(renderer, {
      id: 'agent-map',
      width: 30,
      height: '100%',
      flexDirection: 'column',
      paddingLeft: 1,
      paddingRight: 1,
      border: true,
      borderStyle: 'rounded',
      borderColor: '#475569',
      title: ' Agents ',
    });
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
          fg: '#64748b',
          width: '100%',
        }),
      );
      return;
    }

    const roundNumber = visibleRoundNumber(state);
    this.output.add(
      new TextRenderable(this.renderer, {
        content: roundNumber === null ? 'Run flow' : `Round ${roundNumber} flow`,
        fg: '#cbd5e1',
        width: '100%',
      }),
    );
    for (const [index, phase] of phases.entries()) {
      this.output.add(this.#renderPhase(phase, state.selectedAgentKind === phase.kind));
      if (index < phases.length - 1) {
        this.output.add(
          new TextRenderable(this.renderer, {
            content: '        ↓',
            fg: '#64748b',
            width: '100%',
          }),
        );
      }
    }
  }

  #clear(): void {
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
      borderColor: selected ? '#22d3ee' : '#475569',
      ...(selected ? {backgroundColor: '#0f172a'} : {}),
    });
    const statusColor = STATUS_COLOR[phase.status];
    row.add(
      new TextRenderable(this.renderer, {
        content: `${STATUS_MARKER[phase.status]} ${phase.kind}`,
        fg: selected ? '#f8fafc' : statusColor,
        width: '100%',
      }),
    );
    row.add(
      new TextRenderable(this.renderer, {
        content: phase.status,
        fg: statusColor,
        width: '100%',
      }),
    );
    if (phase.roundLabel) {
      row.add(
        new TextRenderable(this.renderer, {
          content: phase.roundLabel,
          fg: '#94a3b8',
          width: '100%',
        }),
      );
    }
    return row;
  }
}
