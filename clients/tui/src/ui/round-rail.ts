import {BoxRenderable, type CliRenderer, TextRenderable} from '@opentui/core';
import {hasActiveAgentTiming, type RoundState, roundAgentElapsedMs} from '@vibesys/core-state';
import type {SessionController} from '../session-controller.js';
import type {SessionState} from '../session-model.js';
import {
  experimentLogVisible,
  hypothesisRoundFor,
  stripRounds,
  visibleRoundNumber,
} from '../session-model.js';
import {STACKED_WIDTH, TRANSCRIPT_MIN} from './agent-map.js';
import {elapsedLabel} from './previews.js';
import {splitFits} from './right-pane.js';
import type {Theme} from './theme.js';

/** Rail width when it shows the full per-round detail. */
export const RAIL_FULL_WIDTH = 28;
/** Rail width for the narrow fallback: round number and status glyph only. */
export const RAIL_COMPACT_WIDTH = 13;
// The rail takes its column off the agents budget, so it may only appear at a
// width where the agents pane and the transcript both keep their floors beside
// it: rail + STACKED_WIDTH (the agents fallback) + TRANSCRIPT_MIN. Below that the
// rail would push the transcript under its minimum, so it collapses instead.
/** At or above this terminal width the rail shows full rows. */
const RAIL_WIDE_MIN = RAIL_FULL_WIDTH + STACKED_WIDTH + TRANSCRIPT_MIN;
/** Below this terminal width the rail is hidden and the round view is agents + transcript. */
const RAIL_MIN = RAIL_COMPACT_WIDTH + STACKED_WIDTH + TRANSCRIPT_MIN;
/** Border top and bottom; the title rides the top border. */
const RAIL_VCHROME = 2;

const STATUS_GLYPH: Record<RoundState['status'], string> = {
  active: '⟳',
  completed: '✓',
  failed: '✗',
  planned: '·',
};
const STATUS_WORD: Record<RoundState['status'], string> = {
  active: 'run',
  completed: 'done',
  failed: 'fail',
  planned: 'plan',
};

/**
 * A completed round's own status says it ran to the end, not what the judge
 * decided about it, so a round the judge failed would otherwise wear the same
 * solid check as one it passed. It trades the check for a cross instead, on
 * the same reasoning the profile-skipped ring already follows: how the round
 * was judged outranks how it measured, which outranks that it finished. The
 * cross is also the only part of the label compact width keeps, so a narrow
 * rail still carries the verdict. `pass` matches what the check already
 * implies, and `deferred` or an unjudged round has no claim to contradict it,
 * so only `fail` takes the cross.
 */
function statusGlyph(round: RoundState, state: SessionState): string {
  if (round.status === 'completed') {
    if (hypothesisRoundFor(state, round.number)?.judge_verdict === 'fail') return '✗';
    if (round.profileSkipped === true) return '○';
  }
  return STATUS_GLYPH[round.status];
}

/**
 * The rail width for a terminal width, or 0 when the rail should collapse.
 *
 * Wide terminals get the full rail; between the two thresholds it falls back to
 * a compact number-and-glyph column; narrower than that it disappears so the
 * agents graph and transcript keep their width budget.
 */
export function roundRailWidth(terminalWidth: number): number {
  if (terminalWidth >= RAIL_WIDE_MIN) return RAIL_FULL_WIDTH;
  if (terminalWidth >= RAIL_MIN) return RAIL_COMPACT_WIDTH;
  return 0;
}

/**
 * Whether the vertical rail is on screen for this state and width. The round
 * view owns the rail, so the log, a zoomed pane, and an open right-pane split
 * (which takes the row the rail would sit in) all hide it, as does a width below
 * the collapse threshold.
 */
export function roundRailVisible(state: SessionState, terminalWidth: number): boolean {
  if (experimentLogVisible(state)) return false;
  if (state.layout.zoomedPane !== null) return false;
  if (state.layout.right !== null && splitFits(terminalWidth)) return false;
  // Before the run has any rounds there is nothing to rail; the agents graph and
  // transcript keep the whole width rather than losing a column to an empty box.
  if (stripRounds(state).length === 0) return false;
  return roundRailWidth(terminalWidth) > 0;
}

export interface RailWindow {
  rounds: RoundState[];
  hiddenBefore: number;
  hiddenAfter: number;
}

/**
 * The rounds that fit in the rail's rows, always including the selected one.
 *
 * The vertical analogue of the old horizontal window: a run is normally taller
 * than the rail, so the rail is a window onto it that follows the selection and
 * slides by one round as it steps, which reads as the run scrolling past rather
 * than paging. When the run does not fit, two rows are reserved for the
 * `↑ n` / `↓ n` indicators so the counts never overlap a round.
 */
export function railWindow(
  rounds: RoundState[],
  selected: number | null,
  availableRows: number,
  rowHeight = 1,
): RailWindow {
  if (rounds.length === 0 || availableRows <= 0) {
    return {rounds: [], hiddenBefore: 0, hiddenAfter: rounds.length};
  }
  const rows = Math.max(1, Math.floor(availableRows / rowHeight));
  const capacity = rounds.length <= rows ? rows : Math.max(1, rows - 2);
  const index = Math.max(
    0,
    rounds.findIndex(round => round.number === selected),
  );
  let first = index;
  let last = index;
  // Grow outward from the selection, preferring the side that still has rounds,
  // so a selection near either end still fills the rail.
  while (last - first + 1 < capacity) {
    const canBefore = first > 0;
    const canAfter = last < rounds.length - 1;
    if (!canBefore && !canAfter) break;
    const takeAfter = canAfter && (!canBefore || last - index <= index - first);
    if (takeAfter) last += 1;
    else first -= 1;
  }
  return {
    rounds: rounds.slice(first, last + 1),
    hiddenBefore: first,
    hiddenAfter: rounds.length - 1 - last,
  };
}

/**
 * The rounds of a run as a vertical rail on the left of the round view. Rounds
 * read top to bottom, the agents graph and transcript sit to the right, so
 * drilling deeper always moves rightward. Selection is drawn with a marker and
 * the accent surface so it never depends on colour alone.
 */
export class RoundRailView {
  readonly output: BoxRenderable;
  #theme: Theme;
  #renderedState: SessionState | null = null;
  #renderedWidth = 0;
  #renderedRows = 0;
  #elapsedTimer: ReturnType<typeof setInterval> | null = null;
  #runningRound: {round: RoundState; state: SessionState; text: TextRenderable} | null = null;

  constructor(
    private readonly renderer: CliRenderer,
    private readonly controller: SessionController,
    theme: Theme,
  ) {
    this.#theme = theme;
    this.output = new BoxRenderable(renderer, {
      id: 'round-rail',
      width: RAIL_FULL_WIDTH,
      height: '100%',
      flexShrink: 0,
      flexDirection: 'column',
      border: true,
      borderStyle: 'rounded',
      borderColor: theme.borderStrong,
      paddingLeft: 1,
      paddingRight: 1,
      title: ' Rounds ',
      onMouseUp: () => this.controller.focusRound('rounds'),
    });
  }

  applyTheme(theme: Theme): void {
    this.#theme = theme;
    this.output.borderColor = theme.borderStrong;
    this.#renderedState = null;
  }

  /**
   * Draws the rail at `width` columns using `rows` content rows (excluding the
   * border). `width` picks full versus compact rows; both are recomputed from
   * the window every render, so the overflow counts are never stale.
   */
  render(state: SessionState, width: number, rows: number): void {
    if (
      state === this.#renderedState &&
      width === this.#renderedWidth &&
      rows === this.#renderedRows
    ) {
      return;
    }
    this.#renderedState = state;
    this.#renderedWidth = width;
    this.#renderedRows = rows;
    this.output.width = width;
    const focused = state.roundFocus === 'rounds';
    this.output.borderColor = focused ? this.#theme.borderFocus : this.#theme.borderStrong;
    this.output.title = focused ? ' ▸ Rounds ' : ' Rounds ';
    this.#clear();
    const rounds = stripRounds(state);
    if (rounds.length === 0) {
      this.output.add(
        new TextRenderable(this.renderer, {
          content: 'Waiting…',
          fg: this.#theme.textSubtle,
          width: '100%',
        }),
      );
      return;
    }
    const compact = width <= RAIL_COMPACT_WIDTH;
    const selected = visibleRoundNumber(state);
    const runningRound = latestActiveRoundNumber(rounds);
    const available = Math.max(0, rows - RAIL_VCHROME);
    if (available <= 0) {
      // No content rows: the box is all border, so there is nothing to draw and
      // no overflow indicator to place.
      this.#syncElapsedTimer();
      return;
    }
    const view = railWindow(rounds, selected, available);
    // Rounds carry the selection, so they are placed first and never exceed the
    // rows on hand; an overflow indicator is drawn only while a row is still free
    // for it. A one or two row rail therefore never emits more children than it
    // can show, and a rail with no spare row shows no indicator rather than one
    // that would overflow.
    const drawn = view.rounds.slice(0, available);
    let spare = available - drawn.length;
    const showBefore = view.hiddenBefore > 0 && spare > 0;
    if (showBefore) spare -= 1;
    const showAfter = view.hiddenAfter > 0 && spare > 0;
    if (showBefore) this.output.add(this.#indicator(`↑ ${view.hiddenBefore}`));
    for (const round of drawn) {
      this.output.add(this.#renderRound(round, {state, selected, runningRound, compact}));
    }
    if (showAfter) this.output.add(this.#indicator(`↓ ${view.hiddenAfter}`));
    this.#syncElapsedTimer();
  }

  destroy(): void {
    this.#stopElapsedTimer();
  }

  #indicator(content: string): TextRenderable {
    return new TextRenderable(this.renderer, {content, fg: this.#theme.textSubtle, width: '100%'});
  }

  #clear(): void {
    this.#runningRound = null;
    this.#stopElapsedTimer();
    for (const child of [...this.output.getChildren()]) {
      this.output.remove(child);
      child.destroyRecursively();
    }
  }

  #renderRound(
    round: RoundState,
    viewState: {
      state: SessionState;
      selected: number | null;
      runningRound: number | null;
      compact: boolean;
    },
  ): TextRenderable {
    const {state, selected, runningRound, compact} = viewState;
    const isSelected = round.number === selected;
    const isRunning = round.number === runningRound;
    const text = new TextRenderable(this.renderer, {
      content: this.#roundLabel(round, state, isSelected, compact),
      ...this.#roundColors(round, isSelected, isRunning),
      width: '100%',
      onMouseUp: () => {
        this.controller.focusRound('rounds');
        this.controller.selectRound(round.number);
      },
    });
    if (isRunning && hasActiveAgentTiming(round)) this.#runningRound = {round, state, text};
    return text;
  }

  /**
   * The round being viewed is marked twice over: the marker says which one it
   * is, and the accent on its selected surface makes it findable at a glance.
   * Colour alone would fail an operator whose terminal drops it, the marker
   * alone is easy to lose in a long rail.
   */
  #roundColors(
    round: RoundState,
    isSelected: boolean,
    isRunning: boolean,
  ): {fg: string; bg?: string} {
    if (isSelected) return {fg: this.#theme.accent, bg: this.#theme.selectedSurface};
    if (isRunning) return {fg: this.#theme.success};
    if (round.status === 'planned') return {fg: this.#theme.textSubtle};
    if (round.status === 'failed') return {fg: this.#theme.error};
    // A profile-skipped round completed without measuring anything, so it dims
    // like a planned round rather than claiming a fresh result.
    if (round.profileSkipped === true) return {fg: this.#theme.textSubtle};
    return {fg: this.#theme.textPrimary};
  }

  #roundLabel(
    round: RoundState,
    state: SessionState,
    isSelected: boolean,
    compact: boolean,
  ): string {
    const marker = isSelected ? '▸' : ' ';
    const glyph = statusGlyph(round, state);
    if (compact) return `${marker}r${round.number}${glyph}`;
    const metric = roundMetric(round, state, new Date());
    const parts = [`${marker}r${round.number}`, glyph, STATUS_WORD[round.status]];
    if (metric.length > 0) parts.push(metric);
    return parts.join(' ');
  }

  #syncElapsedTimer(): void {
    if (this.#runningRound === null || this.#elapsedTimer !== null) return;
    this.#elapsedTimer = setInterval(() => {
      if (this.#runningRound === null) return;
      const {round, state, text} = this.#runningRound;
      text.content = this.#roundLabel(
        round,
        state,
        round.number === visibleRoundNumber(state),
        false,
      );
    }, 1000);
  }

  #stopElapsedTimer(): void {
    if (this.#elapsedTimer === null) return;
    clearInterval(this.#elapsedTimer);
    this.#elapsedTimer = null;
  }
}

/**
 * The per-round metric shown after the status word: the live elapsed time while
 * a round runs, its measured delta once resolved (the value the experiments
 * table reports, read from the same experiment-log record so the two cannot
 * disagree), or its wall duration when no delta was measured. Planned rounds
 * have nothing to measure.
 */
function roundMetric(round: RoundState, state: SessionState, now: Date): string {
  if (round.status === 'planned') return '';
  if (round.status === 'active') return elapsedLabel(roundAgentElapsedMs(round, now));
  const delta = hypothesisRoundFor(state, round.number)?.perf_delta_pct;
  if (typeof delta === 'number') {
    return `${delta > 0 ? '+' : ''}${delta.toFixed(Math.abs(delta) >= 10 ? 0 : 1)}%`;
  }
  const end = round.finishedAt ? new Date(round.finishedAt) : now;
  return elapsedLabel(roundAgentElapsedMs(round, end));
}

function latestActiveRoundNumber(rounds: RoundState[]): number | null {
  return [...rounds].reverse().find(round => round.status === 'active')?.number ?? null;
}
