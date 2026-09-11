import {
  BoxRenderable,
  bold,
  type CliRenderer,
  fg,
  type StyledText,
  type TextChunk,
  TextRenderable,
  t,
} from '@opentui/core';
import {hasActiveAgentTiming, type RoundState, roundAgentElapsedMs} from '@vibesys/core-state';
import type {SessionController} from '../session-controller.js';
import type {SessionState} from '../session-model.js';
import {hypothesisRoundFor, stripRounds, visibleRoundNumber} from '../session-model.js';
import {elapsedLabel} from './previews.js';
import {displayWidth} from './text-width.js';
import {ensureContrast, type Theme} from './theme.js';

/**
 * What a round came to, which is what its glyph shows.
 *
 * A completed round's own status says it ran to the end, not what the judge
 * decided about it, so a round the judge failed would otherwise wear the same
 * solid check as one it passed. How the round was judged outranks how it
 * measured (profile skipped), which outranks that it finished. `pass` matches
 * what the check already implies, and `deferred` or an unjudged round has no
 * claim to contradict it, so only `fail` takes the cross.
 */
export type RoundOutcome = 'done' | 'fail' | 'skipped' | 'live' | 'planned';

export const OUTCOME_GLYPH: Record<RoundOutcome, string> = {
  done: '✓',
  fail: '✗',
  skipped: '○',
  live: '⟳',
  planned: '·',
};

export function roundOutcome(round: RoundState, state: SessionState): RoundOutcome {
  switch (round.status) {
    case 'active':
      return 'live';
    case 'planned':
      return 'planned';
    case 'failed':
      return 'fail';
    case 'completed':
      if (hypothesisRoundFor(state, round.number)?.judge_verdict === 'fail') return 'fail';
      return round.profileSkipped === true ? 'skipped' : 'done';
  }
}

/**
 * The live elapsed time while a round runs, its measured delta once resolved
 * (the value the experiments table reports, read from the same experiment-log
 * record so the two cannot disagree), or its wall duration when no delta was
 * measured. Planned rounds have nothing to measure.
 */
export function roundMetric(round: RoundState, state: SessionState, now: Date): string {
  if (round.status === 'planned') return '';
  if (round.status === 'active') return elapsedLabel(roundAgentElapsedMs(round, now));
  const delta = hypothesisRoundFor(state, round.number)?.perf_delta_pct;
  if (typeof delta === 'number') {
    return `${delta > 0 ? '+' : ''}${delta.toFixed(Math.abs(delta) >= 10 ? 0 : 1)}%`;
  }
  const end = round.finishedAt ? new Date(round.finishedAt) : now;
  return elapsedLabel(roundAgentElapsedMs(round, end));
}

export function latestActiveRoundNumber(rounds: RoundState[]): number | null {
  return [...rounds].reverse().find(round => round.status === 'active')?.number ?? null;
}

/** What one tab says about its round, before a narrow width trims it. */
export interface RoundTab {
  number: number;
  outcome: RoundOutcome;
  metric: string;
}

/** A failed or skipped round names its outcome in place of a metric. */
export function roundTab(round: RoundState, state: SessionState, now: Date): RoundTab {
  const outcome = roundOutcome(round, state);
  const metric =
    outcome === 'fail' || outcome === 'skipped' ? outcome : roundMetric(round, state, now);
  return {number: round.number, outcome, metric};
}

/** Columns between two slots. */
const GAP = 1;
/** Columns between an overflow marker and the slot next to it. */
const MARKER_GAP = 2;
/** The margin cell before the bar and the cell it leaves free at the right edge. */
const EDGE = 2;

const beforeMarker = (hidden: number): string => `‹ ${hidden}`;
const afterMarker = (hidden: number): string => `${hidden} ›`;

/** The slots `first..last` of a `TabWindow`, both inclusive. */
export interface TabWindow {
  first: number;
  last: number;
}

/**
 * The slots that fit in `width` columns, holding the `selected` slot and, when
 * given, the `live` one; null when they cannot both fit.
 *
 * Grows toward the live slot first, then outward from the selection, taking the
 * later side on a tie. An overflow marker is reserved at its real width and
 * only for a side that is still hidden.
 */
export function tabWindow(
  widths: readonly number[],
  selected: number,
  live: number | null,
  width: number,
): TabWindow | null {
  const count = widths.length;
  // ponytail: re-sums the window per probe, O(n^2) in rounds; fine at run sizes.
  const fits = (first: number, last: number): boolean => {
    let used = GAP * (last - first);
    for (let index = first; index <= last; index += 1) used += widths[index] ?? 0;
    if (first > 0) used += beforeMarker(first).length + MARKER_GAP;
    if (last < count - 1) used += MARKER_GAP + afterMarker(count - 1 - last).length;
    return used <= width - EDGE;
  };
  if (fits(0, count - 1)) return {first: 0, last: count - 1};
  if (!fits(selected, selected)) return null;
  let first = selected;
  let last = selected;
  while (live !== null && live > last) {
    if (!fits(first, last + 1)) return null;
    last += 1;
  }
  while (live !== null && live < first) {
    if (!fits(first - 1, last)) return null;
    first -= 1;
  }
  for (;;) {
    const canAfter = last < count - 1 && fits(first, last + 1);
    const canBefore = first > 0 && fits(first - 1, last);
    if (!canAfter && !canBefore) return {first, last};
    if (canAfter && (!canBefore || last - selected <= selected - first)) last += 1;
    else first -= 1;
  }
}

/**
 * Narrow-width levels, widest first: L1 drops the metric of a tab that is
 * neither selected nor live, L2 halves the padding, L3 drops the selected
 * tab's metric, L4 drops the spaces inside the label. The live tab keeps its
 * timer at every level.
 */
const LEVELS = [0, 1, 2, 3, 4] as const;
type Level = (typeof LEVELS)[number];

interface Slot {
  tab: RoundTab;
  selected: boolean;
  content: StyledText;
  width: number;
}

function tabColors(
  outcome: RoundOutcome,
  selected: boolean,
  theme: Theme,
): {number: string; glyph: string; metric: string} {
  const number = selected
    ? theme.textStrong
    : outcome === 'skipped' || outcome === 'planned'
      ? theme.textSubtle
      : theme.textMuted;
  switch (outcome) {
    case 'done':
      return {number, glyph: theme.success, metric: selected ? theme.textPrimary : theme.textMuted};
    case 'fail':
      return {number, glyph: theme.error, metric: theme.error};
    case 'live':
      return {number, glyph: theme.warning, metric: theme.warning};
    case 'skipped':
    case 'planned':
      return {number, glyph: theme.textSubtle, metric: theme.textSubtle};
  }
}

/** `  r6 ✓ +1%  `, or `▎ r6 ✓ +1%  ` when selected, trimmed to `level`. */
function slot(tab: RoundTab, level: Level, selected: boolean, live: boolean, theme: Theme): Slot {
  const pad = level >= 2 ? ' ' : '  ';
  const space = level >= 4 ? '' : ' ';
  const metric = tab.metric !== '' && (live || level < (selected ? 3 : 1));
  const colors = tabColors(tab.outcome, selected, theme);
  // The theme holds its colours to the floor against the canvas only, so each
  // one is lifted again to read on the selection fill.
  const ink = (color: string, text: string, strong = false): TextChunk => {
    const chunk = fg(
      selected ? ensureContrast(color, theme.selectedSurface, theme.minContrast) : color,
    )(text);
    return strong ? bold(chunk) : chunk;
  };
  const content = t`${selected ? ink(theme.accent, `▎${pad.slice(1)}`) : pad}${ink(
    colors.number,
    `r${tab.number}`,
    selected,
  )}${space}${ink(colors.glyph, OUTCOME_GLYPH[tab.outcome], selected)}${
    metric ? ink(colors.metric, `${space}${tab.metric}`) : ''
  }${pad}`;
  const width = displayWidth(content.chunks.map(chunk => chunk.text).join(''));
  return {tab, selected, content, width};
}

/**
 * The rounds of a run as one row of tabs across the top of the round view.
 * The selected tab carries the selection fill and an accent bar on its leading
 * edge, so it never depends on colour alone; the live round keeps its own
 * colour and timer whether selected or not, so the two can differ and both
 * stay readable.
 */
export class RoundTabsView {
  readonly output: BoxRenderable;
  #theme: Theme;
  #renderedState: SessionState | null = null;
  #renderedWidth = 0;
  #elapsedTimer: ReturnType<typeof setTimeout> | null = null;

  constructor(
    private readonly renderer: CliRenderer,
    private readonly controller: SessionController,
    theme: Theme,
  ) {
    this.#theme = theme;
    this.output = new BoxRenderable(renderer, {
      id: 'round-tabs',
      width: '100%',
      height: 1,
      flexShrink: 0,
      flexDirection: 'row',
      paddingLeft: 1,
      overflow: 'hidden',
    });
  }

  applyTheme(theme: Theme): void {
    this.#theme = theme;
    this.#renderedState = null;
  }

  /** Draws the bar at `width` columns; returns the rows it takes, 0 before any round exists. */
  render(state: SessionState, width: number): number {
    if (state !== this.#renderedState || width !== this.#renderedWidth) {
      this.#renderedState = state;
      this.#renderedWidth = width;
      this.#draw(state, width);
    }
    return this.output.visible ? 1 : 0;
  }

  /** Takes the bar off screen, timer included, until the next `render` draws it afresh. */
  hide(): void {
    this.#stopElapsedTimer();
    this.#renderedState = null;
    this.output.visible = false;
  }

  destroy(): void {
    this.#stopElapsedTimer();
  }

  #draw(state: SessionState, width: number): void {
    this.#stopElapsedTimer();
    for (const child of [...this.output.getChildren()]) {
      this.output.remove(child);
      child.destroyRecursively();
    }
    const rounds = stripRounds(state);
    this.output.visible = rounds.length > 0;
    if (rounds.length === 0) return;
    this.output.width = width;
    const now = new Date();
    const tabs = rounds.map(round => roundTab(round, state, now));
    const selectedNumber = visibleRoundNumber(state);
    const liveNumber = latestActiveRoundNumber(rounds);
    const selected = Math.max(
      0,
      tabs.findIndex(tab => tab.number === selectedNumber),
    );
    const liveIndex = tabs.findIndex(tab => tab.number === liveNumber);
    const live = liveIndex >= 0 ? liveIndex : null;

    let slots: Slot[] = [];
    let view: TabWindow | null = null;
    for (const level of LEVELS) {
      slots = tabs.map((tab, index) =>
        slot(tab, level, tab.number === selectedNumber, index === live, this.#theme),
      );
      view = tabWindow(
        slots.map(each => each.width),
        selected,
        live,
        width,
      );
      if (view !== null) break;
    }
    // Not even L4 holds both: keep the selected round and let the live one go.
    view ??= tabWindow(
      slots.map(each => each.width),
      selected,
      null,
      width,
    );
    // Still nothing: the selected slot alone, clipped by the box, no markers.
    const first = view?.first ?? selected;
    const last = view?.last ?? selected;
    if (view !== null && first > 0) {
      this.output.add(this.#marker(beforeMarker(first), {marginRight: MARKER_GAP}));
    }
    slots.slice(first, last + 1).forEach((each, offset) => {
      this.output.add(this.#slot(each, offset > 0));
    });
    if (view !== null && last < tabs.length - 1) {
      this.output.add(this.#marker(afterMarker(tabs.length - 1 - last), {marginLeft: MARKER_GAP}));
    }

    const liveRound = rounds.find(round => round.number === liveNumber);
    if (liveRound !== undefined && hasActiveAgentTiming(liveRound)) {
      // Label widths change at 9s -> 10s and 59s -> 1m 0s, so a tick re-runs the
      // whole layout rather than rewriting one label.
      this.#elapsedTimer = setTimeout(() => this.#draw(state, width), 1000);
    }
  }

  #slot({tab, selected, content, width}: Slot, afterAnother: boolean): TextRenderable {
    return new TextRenderable(this.renderer, {
      content,
      width,
      flexShrink: 0,
      wrapMode: 'none',
      marginLeft: afterAnother ? GAP : 0,
      ...(selected ? {bg: this.#theme.selectedSurface} : {}),
      // The bar is not a pane and takes no keys, so a click moves no focus.
      onMouseUp: () => this.controller.selectRound(tab.number),
    });
  }

  #marker(text: string, margin: {marginLeft: number} | {marginRight: number}): TextRenderable {
    return new TextRenderable(this.renderer, {
      content: text,
      fg: this.#theme.textSubtle,
      width: displayWidth(text),
      flexShrink: 0,
      ...margin,
    });
  }

  #stopElapsedTimer(): void {
    if (this.#elapsedTimer === null) return;
    clearTimeout(this.#elapsedTimer);
    this.#elapsedTimer = null;
  }
}
