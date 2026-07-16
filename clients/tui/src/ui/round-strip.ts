import {BoxRenderable, type CliRenderer, TextRenderable} from '@opentui/core';
import type {RoundSummary} from '../run-map.js';
import type {SessionController} from '../session-controller.js';
import type {SessionState} from '../session-model.js';
import {visibleRoundNumber} from '../session-model.js';

const ROUND_MARKER: Record<RoundSummary['status'], string> = {
  active: '●',
  completed: '✓',
  failed: '×',
};

export class RoundStripView {
  readonly output: BoxRenderable;
  #renderedState: SessionState | null = null;

  constructor(
    private readonly renderer: CliRenderer,
    private readonly controller: SessionController,
  ) {
    this.output = new BoxRenderable(renderer, {
      id: 'round-strip',
      width: '100%',
      height: 3,
      border: true,
      borderStyle: 'rounded',
      borderColor: '#334155',
      paddingLeft: 1,
      paddingRight: 1,
      title: ' Rounds ',
    });
  }

  render(state: SessionState): void {
    if (state === this.#renderedState) return;
    this.#renderedState = state;
    this.#clear();
    if (state.rounds.length === 0) {
      this.output.add(
        new TextRenderable(this.renderer, {
          content: 'Waiting for rounds...',
          fg: '#64748b',
          width: '100%',
        }),
      );
      return;
    }
    const row = new BoxRenderable(this.renderer, {
      id: 'round-strip-row',
      width: '100%',
      flexDirection: 'row',
    });
    const selected = visibleRoundNumber(state);
    for (const round of state.rounds.slice(-8)) row.add(this.#renderRound(round, selected));
    this.output.add(row);
  }

  #clear(): void {
    for (const child of [...this.output.getChildren()]) {
      this.output.remove(child);
      child.destroyRecursively();
    }
  }

  #renderRound(round: RoundSummary, selected: number | null): TextRenderable {
    const isSelected = round.number === selected;
    const label = `${isSelected ? '[' : ' '} ${round.number}${ROUND_MARKER[round.status]} ${isSelected ? ']' : ' '}`;
    return new TextRenderable(this.renderer, {
      content: label,
      fg: isSelected ? '#f8fafc' : '#cbd5e1',
      ...(isSelected ? {bg: '#0f172a'} : {}),
      onMouseUp: () => this.controller.selectRound(round.number),
    });
  }
}
