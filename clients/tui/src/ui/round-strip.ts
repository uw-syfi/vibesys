import {BoxRenderable, type CliRenderer, TextRenderable} from '@opentui/core';
import type {RoundSummary} from '../run-map.js';
import type {SessionController} from '../session-controller.js';
import type {SessionState} from '../session-model.js';
import {visibleRoundNumber} from '../session-model.js';

const ACTIVE_ROUND_COLOR = '#22c55e';
const DEFAULT_ROUND_COLOR = '#cbd5e1';

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
    const runningRound = latestActiveRoundNumber(state.rounds);
    for (const round of state.rounds.slice(-8)) {
      row.add(this.#renderRound(round, {selected, runningRound}));
    }
    this.output.add(row);
  }

  #clear(): void {
    for (const child of [...this.output.getChildren()]) {
      this.output.remove(child);
      child.destroyRecursively();
    }
  }

  #renderRound(
    round: RoundSummary,
    viewState: {selected: number | null; runningRound: number | null},
  ): TextRenderable {
    const {selected, runningRound} = viewState;
    const isSelected = round.number === selected;
    const isRunning = round.number === runningRound;
    return new TextRenderable(this.renderer, {
      content: this.#roundLabel(round, selected),
      fg: isRunning ? ACTIVE_ROUND_COLOR : DEFAULT_ROUND_COLOR,
      ...(isSelected ? {bg: '#0f172a'} : {}),
      onMouseUp: () => this.controller.selectRound(round.number),
    });
  }

  #roundLabel(round: RoundSummary, selected: number | null): string {
    const isSelected = round.number === selected;
    return `${isSelected ? '[' : ' '} r${round.number} ${isSelected ? ']' : ' '}`;
  }
}

function latestActiveRoundNumber(rounds: RoundSummary[]): number | null {
  return [...rounds].reverse().find(round => round.status === 'active')?.number ?? null;
}
