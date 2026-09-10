import {BoxRenderable, type CliRenderer, TextRenderable} from '@opentui/core';
import {
  type AgentPhase,
  hasActiveAgentTiming,
  phasesForRound,
  type RoundState,
  roundAgentElapsedMs,
} from '@vibesys/core-state';
import type {SessionController} from '../session-controller.js';
import type {SessionState} from '../session-model.js';
import {
  experimentLogVisible,
  hypothesisRoundFor,
  stripRounds,
  visibleRoundNumber,
} from '../session-model.js';
import {SPINNER_FRAMES, SPINNER_INTERVAL_MS} from './activity-bar.js';
import {
  agentGraphEnabled,
  STACKED_WIDTH,
  STATUS_MARKER,
  statusColor,
  TRANSCRIPT_MIN,
} from './agent-map.js';
import {elapsedLabel} from './previews.js';
import {splitFits} from './right-pane.js';
import {displayWidth, truncateToWidth} from './text-width.js';
import type {Theme} from './theme.js';

/**
 * Rail width when it shows the full per-round detail. Six columns wider than
 * the rounds-only rail needed, because an expanded round's agent rows are the
 * widest thing the rail draws: two columns of indent, a status glyph, the agent
 * kind, and the elapsed time.
 */
export const RAIL_FULL_WIDTH = 34;
/**
 * The rail's width while the graph pane is on. It lists rounds alone then, so
 * it keeps the width it had before agent rows existed: the graph is opted into
 * in order to be compared against, and a comparison is worth nothing if turning
 * it on also costs it the columns it needs to draw.
 */
const RAIL_ROUNDS_ONLY_WIDTH = 28;
/** Rail width for the narrow fallback: round number and status glyph only. */
export const RAIL_COMPACT_WIDTH = 13;
// The rail's column comes off whatever else the round view is drawing. With the
// graph pane on, that is the agents pane and the transcript, and the rail may
// only appear where both keep their floors beside it. With the graph off there
// is no agents pane to budget for, so the rail needs only the transcript floor
// and survives to much narrower terminals.
/** Columns the round view owes the agents pane, 0 while the graph is off. */
function agentsBudget(): number {
  return agentGraphEnabled() ? STACKED_WIDTH : 0;
}
/** Border top and bottom; the title rides the top border. */
const RAIL_VCHROME = 2;
/** Indent, glyph and a space before an agent row's kind. */
const AGENT_INDENT = '  ';

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
 * transcript keeps its floor, along with the agents pane when that is on.
 */
export function roundRailWidth(terminalWidth: number): number {
  const full = agentGraphEnabled() ? RAIL_ROUNDS_ONLY_WIDTH : RAIL_FULL_WIDTH;
  const budget = agentsBudget() + TRANSCRIPT_MIN;
  if (terminalWidth >= full + budget) return full;
  if (terminalWidth >= RAIL_COMPACT_WIDTH + budget) return RAIL_COMPACT_WIDTH;
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
  rowsFor: (round: RoundState) => number = () => 1,
): RailWindow {
  if (rounds.length === 0 || availableRows <= 0) {
    return {rounds: [], hiddenBefore: 0, hiddenAfter: rounds.length};
  }
  const total = rounds.reduce((sum, round) => sum + rowsFor(round), 0);
  // Two rows go to the overflow indicators, and only when the run does not fit.
  const budget = total <= availableRows ? availableRows : Math.max(1, availableRows - 2);
  const index = Math.max(
    0,
    rounds.findIndex(round => round.number === selected),
  );
  let first = index;
  let last = index;
  // The selection is always in the window even when it alone overruns the rail;
  // the caller clamps what it draws, so a rail too short for one expanded round
  // shows the top of it rather than nothing.
  let used = rowsFor(rounds[index] as RoundState);
  // Grow outward from the selection, preferring the side that still has rounds,
  // so a selection near either end still fills the rail. Rounds are no longer a
  // uniform row tall (an expanded one carries its agents), so growth is measured
  // in rows and tries the other side when the preferred one does not fit.
  for (;;) {
    const sides: boolean[] = [];
    const canBefore = first > 0;
    const canAfter = last < rounds.length - 1;
    if (!canBefore && !canAfter) break;
    const preferAfter = canAfter && (!canBefore || last - index <= index - first);
    sides.push(preferAfter);
    if (canBefore && canAfter) sides.push(!preferAfter);
    const grew = sides.some(takeAfter => {
      const next = rounds[takeAfter ? last + 1 : first - 1];
      if (next === undefined) return false;
      const cost = rowsFor(next);
      if (used + cost > budget) return false;
      used += cost;
      if (takeAfter) last += 1;
      else first -= 1;
      return true;
    });
    if (!grew) break;
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
  /**
   * Rows carrying a running clock, each with how to recompute its own label.
   * One list rather than a single round: an expanded round's active agent ticks
   * too, and two mechanisms for one clock is one more than the rail needs.
   */
  #ticking: Array<{text: TextRenderable; content: () => string}> = [];
  /** Advances every tick; an active agent's marker indexes it into `SPINNER_FRAMES`. */
  #frame = 0;

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
    // An expanded round lists the agents that ran in it directly under it: the
    // last rung of the run > hypothesis > round > agents hierarchy the rail
    // already draws, rather than a second pane to the right of it.
    const agentsOf = (round: RoundState): AgentPhase[] =>
      state.expandedRounds.includes(round.number)
        ? phasesForRound(state.core.phases, round.number)
        : [];
    const view = railWindow(rounds, selected, available, round => 1 + agentsOf(round).length);
    // The budget is rows, not rounds, because a round is no longer one row tall.
    // Rounds carry the selection, so they are placed first and never exceed the
    // rows on hand; an overflow indicator is drawn only while a row is still free
    // for it. A short rail therefore never emits more children than it can show,
    // and a rail with no spare row shows no indicator rather than one that would
    // overflow.
    const drawn: TextRenderable[] = [];
    for (const round of view.rounds) {
      if (drawn.length >= available) break;
      drawn.push(this.#renderRound(round, {state, selected, runningRound, compact}));
      for (const phase of agentsOf(round)) {
        if (drawn.length >= available) break;
        drawn.push(this.#renderAgent(phase, state, compact));
      }
    }
    let spare = available - drawn.length;
    const showBefore = view.hiddenBefore > 0 && spare > 0;
    if (showBefore) spare -= 1;
    const showAfter = view.hiddenAfter > 0 && spare > 0;
    if (showBefore) this.output.add(this.#indicator(`↑ ${view.hiddenBefore}`));
    for (const row of drawn) this.output.add(row);
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
    this.#ticking = [];
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
    if (isRunning && hasActiveAgentTiming(round)) {
      this.#ticking.push({
        // The width is read at tick time, not captured: a resize between ticks
        // changes which label this row should be showing.
        content: () =>
          this.#roundLabel(
            round,
            state,
            round.number === visibleRoundNumber(state),
            this.#renderedWidth <= RAIL_COMPACT_WIDTH,
          ),
        text,
      });
    }
    return text;
  }

  /**
   * One agent of an expanded round. Selection reuses the transcript's existing
   * agent filter rather than a second one: clicking a row filters the transcript
   * to that agent and clicking it again clears the filter, which is what the
   * graph's nodes did. Focus stays on the rail, because with the graph off the
   * rail is where the operator is.
   */
  #renderAgent(phase: AgentPhase, state: SessionState, compact: boolean): TextRenderable {
    const selected = state.selectedAgentKind === phase.kind;
    const label = (): string =>
      agentLabel(phase, this.#innerWidth(), compact, new Date(), this.#frame);
    const text = new TextRenderable(this.renderer, {
      content: label(),
      fg: selected ? this.#theme.accent : statusColor(this.#theme, phase.status),
      ...(selected ? {bg: this.#theme.selectedSurface} : {}),
      width: '100%',
      onMouseUp: () => {
        this.controller.selectAgent(phase.kind);
        this.controller.focusRound('rounds');
      },
    });
    // Only a running agent has a clock to advance; a finished one's duration is
    // already final, and only a running agent's marker spins.
    if (phase.status === 'active') this.#ticking.push({content: label, text});
    return text;
  }

  /** Columns a row has after the rail's border and padding. */
  #innerWidth(): number {
    return Math.max(1, this.#renderedWidth - 4);
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
    if (this.#ticking.length === 0 || this.#elapsedTimer !== null) return;
    // One timer for both jobs: elapsed-time text and the active-agent spinner
    // both refresh off this tick rather than each owning one. SPINNER_INTERVAL_MS
    // is the app's one animation cadence (activity-bar.ts), not a value picked
    // for this rail alone.
    this.#elapsedTimer = setInterval(() => {
      this.#frame = (this.#frame + 1) % SPINNER_FRAMES.length;
      for (const row of this.#ticking) row.text.content = row.content();
    }, SPINNER_INTERVAL_MS);
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

/**
 * An agent as a rail row: status glyph, kind, then the status word and the time
 * the agent ran, right aligned. The glyph carries status without colour and uses
 * the same vocabulary the graph's nodes did, so an operator who learned it there
 * reads it here. The word is dropped before the time when the rail is too narrow
 * for both, because the glyph already says what the word says. An active agent's
 * glyph animates (`agentMarker`); every other status keeps the static one.
 */
function agentLabel(
  phase: AgentPhase,
  inner: number,
  compact: boolean,
  now: Date,
  frame: number,
): string {
  const head = `${AGENT_INDENT}${agentMarker(phase.status, frame)} ${phase.kind}`;
  if (compact) return truncateToWidth(head, inner);
  const timing = agentElapsed(phase, now);
  const headWidth = displayWidth(head);
  for (const tail of [[phase.status, timing].filter(part => part.length > 0).join(' '), timing]) {
    if (tail.length === 0) continue;
    const gap = inner - headWidth - displayWidth(tail);
    if (gap >= 1) return `${head}${' '.repeat(gap)}${tail}`;
  }
  return truncateToWidth(head, inner);
}

/**
 * The status glyph for an agent row. Active is the one status still changing,
 * so it is the one that animates: `frame` indexes the same braille spinner
 * every other in-flight indicator in the app already shows (`activity-bar.ts`),
 * off the rail's own tick rather than a second timer. Every other status keeps
 * its static `STATUS_MARKER` glyph, tick to tick, so a finished or waiting
 * agent never appears to still be doing something.
 */
export function agentMarker(status: AgentPhase['status'], frame: number): string {
  if (status !== 'active') return STATUS_MARKER[status];
  return SPINNER_FRAMES[frame % SPINNER_FRAMES.length] ?? STATUS_MARKER.active;
}

/**
 * How long an agent has been running, or ran for. Read off the phase's own
 * stamps rather than the round's, so a round with several agents reports each
 * one separately. A phase with no start stamp, or stamps that do not parse,
 * reports nothing rather than a wrong number.
 */
function agentElapsed(phase: AgentPhase, now: Date): string {
  if (phase.startedAt === undefined) return '';
  const start = new Date(phase.startedAt).getTime();
  const end = phase.finishedAt === undefined ? now.getTime() : new Date(phase.finishedAt).getTime();
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return '';
  return elapsedLabel(end - start);
}

function latestActiveRoundNumber(rounds: RoundState[]): number | null {
  return [...rounds].reverse().find(round => round.status === 'active')?.number ?? null;
}
