import {BoxRenderable, type CliRenderer, ScrollBoxRenderable, TextRenderable} from '@opentui/core';
import type {HypothesisEntry} from '../protocol.js';
import type {SessionController} from '../session-controller.js';
import {entryKey, experimentLogVisible, type SessionState} from '../session-model.js';
import type {Theme} from './theme.js';

const MIN_BODY_WIDTH = 40;
/**
 * Border, horizontal padding, and the scrollbar gutter. The gutter is reserved
 * even when the log fits, so rows do not reflow the moment it starts to scroll.
 */
const PANEL_CHROME_COLUMNS = 5;
/** App header, panel border, column header, footer, key help, and input box. */
const CHROME_ROWS = 10;
const MIN_VIEWPORT_ROWS = 3;
const HINT_MIN_WIDTH = 60;

/**
 * Columns past the identity pair are dropped as the terminal narrows, widest
 * first, so hypothesis and rounds always survive.
 */
const CLAIM_MIN_WIDTH = 90;
const MEASURED_MIN_WIDTH = 62;
const KEPT_MIN_WIDTH = 104;

/**
 * Panel width at which the table still shows the claim, which is the row's
 * identity in words. Anything that takes columns from the table reads this
 * rather than restating the threshold.
 */
export const LOG_CLAIM_PANEL_WIDTH = CLAIM_MIN_WIDTH + PANEL_CHROME_COLUMNS;
/** Panel width that still carries the measured and verdict columns. */
export const LOG_COMPACT_PANEL_WIDTH = MEASURED_MIN_WIDTH + PANEL_CHROME_COLUMNS;

interface Columns {
  claim: boolean;
  measured: boolean;
  verdict: boolean;
  kept: boolean;
  claimWidth: number;
}

export class ExperimentLogView {
  readonly output: BoxRenderable;
  readonly #header: TextRenderable;
  readonly #rows: ScrollBoxRenderable;
  readonly #footerLine: TextRenderable;
  #theme: Theme;
  #renderedState: SessionState | null = null;
  #renderedWidth = 0;
  #availableWidth: number | null = null;

  constructor(
    private readonly renderer: CliRenderer,
    private readonly controller: SessionController,
    theme: Theme,
  ) {
    this.#theme = theme;
    this.output = new BoxRenderable(renderer, {
      id: 'experiment-log',
      width: '100%',
      flexGrow: 1,
      flexDirection: 'column',
      paddingLeft: 1,
      paddingRight: 1,
      border: true,
      borderStyle: 'rounded',
      borderColor: theme.accent,
      backgroundColor: theme.elevatedSurface,
      visible: false,
      title: ' Experiments ',
    });
    this.#header = new TextRenderable(renderer, {
      content: '',
      fg: theme.textSubtle,
      width: '100%',
      height: 1,
      flexShrink: 0,
      wrapMode: 'none',
      truncate: true,
    });
    // A scroll box rather than a hand-rolled window: it gives wheel and
    // trackpad scrolling for free, and keeps the whole log reachable instead
    // of only the rows around the selection.
    this.#rows = new ScrollBoxRenderable(renderer, {
      id: 'experiment-rows',
      width: '100%',
      flexGrow: 1,
      flexShrink: 1,
      minHeight: 1,
      stickyScroll: false,
      viewportCulling: true,
      verticalScrollbarOptions: {showArrows: false},
    });
    this.#footerLine = new TextRenderable(renderer, {
      content: '',
      fg: theme.textSubtle,
      width: '100%',
      height: 1,
      flexShrink: 0,
      wrapMode: 'none',
      truncate: true,
    });
    this.output.add(this.#header);
    this.output.add(this.#rows);
    this.output.add(this.#footerLine);
  }

  /**
   * Columns the log actually has. Set when a visualization pane shares the
   * row, so the table drops columns for the space it has rather than for the
   * whole terminal.
   */
  setAvailableWidth(width: number | null): void {
    this.#availableWidth = width;
  }

  applyTheme(theme: Theme): void {
    this.#theme = theme;
    this.output.borderColor = theme.accent;
    this.output.backgroundColor = theme.elevatedSurface;
    this.#header.fg = theme.textSubtle;
    this.#footerLine.fg = theme.textSubtle;
    this.#renderedState = null;
  }

  render(state: SessionState): void {
    const log = experimentLogVisible(state) ? state.experimentLog : null;
    if (log === null) {
      this.output.visible = false;
      this.#renderedState = null;
      return;
    }
    this.output.visible = true;
    const width = this.#availableWidth ?? this.renderer.terminalWidth;
    if (state === this.#renderedState && width === this.#renderedWidth) return;
    this.#renderedState = state;
    this.#renderedWidth = width;
    this.#clear();

    if (log.error !== null) {
      this.#header.content = '';
      this.#line(log.error, this.#theme.conversation.failure.content);
      this.#footerLine.content = '';
      return;
    }
    if (log.pending) {
      this.#header.content = '';
      this.#line('Loading experiments...', this.#theme.textSubtle);
      this.#footerLine.content = '';
      return;
    }
    if (log.entries.length === 0) {
      this.#header.content = '';
      this.#line('No hypotheses have been recorded yet.', this.#theme.textSubtle);
      this.#footerLine.content = 'The first one appears once the orchestrator has planned a round.';
      return;
    }
    this.#renderTable(log.entries, log.selectedId);
  }

  #renderTable(entries: HypothesisEntry[], selectedId: string | null): void {
    const columns = resolveColumns(this.#bodyWidth());
    const keys = entries.map(entryKey);
    const selected = selectedId === null ? 0 : Math.max(0, keys.indexOf(selectedId));

    this.#header.content = headerRow(columns);
    for (const [index, entry] of entries.entries()) {
      this.#row(entry, columns, keys[index] === selectedId, index);
    }
    // Keyboard selection nudges the scroll position only when the row would
    // otherwise be off screen, so the wheel stays in charge the rest of the
    // time. Computed rather than deferred to scrollChildIntoView, which needs
    // a layout pass the freshly added rows have not had yet.
    this.#followSelection(selected, entries.length);
    const position = `${selected + 1}/${entries.length}`;
    // The hint is the first thing to give up room; truncating it mid-word
    // reads as breakage rather than as a narrow terminal.
    const hint =
      this.#bodyWidth() >= HINT_MIN_WIDTH
        ? '↑↓ or scroll: select · Enter or /open-round: open its rounds'
        : '↑↓ · Enter';
    this.#footerLine.content = `${position} · ${hint}`;
  }

  #row(entry: HypothesisEntry, columns: Columns, isSelected: boolean, index: number): void {
    const cells = entryCells(entry, columns);
    // The active hypothesis is called out on its own, so it stays visible
    // whether or not it happens to be the selected row.
    const base = entry.active === true ? this.#theme.warning : this.#theme.textPrimary;
    const selection = isSelected ? {backgroundColor: this.#theme.selectedSurface} : {};
    const row = new BoxRenderable(this.renderer, {
      id: rowId(index),
      width: '100%',
      height: 1,
      flexShrink: 0,
      flexDirection: 'row',
      ...selection,
      onMouseUp: () => {
        this.controller.moveExperimentSelection(index - this.#selectedIndex());
      },
    });
    // The outcome is its own renderable so the resolution can carry a color of
    // its own without recoloring the row. Status stays legible without it:
    // the word is spelled out, exactly as the theme work requires.
    row.add(this.#cell(cells.leading, base, isSelected));
    row.add(this.#cell(cells.outcome, outcomeColor(this.#theme, entry), isSelected));
    if (cells.trailing) row.add(this.#cell(cells.trailing, base, isSelected));
    this.#rows.add(row);
  }

  #cell(content: string, fg: string, isSelected: boolean): TextRenderable {
    return new TextRenderable(this.renderer, {
      content,
      fg,
      ...(isSelected ? {bg: this.#theme.selectedSurface} : {}),
      wrapMode: 'none',
      truncate: true,
    });
  }

  #followSelection(selected: number, total: number): void {
    const viewport = this.#viewportRows();
    const top = Math.min(this.#rows.scrollTop, Math.max(0, total - viewport));
    if (selected < top) this.#rows.scrollTo(selected);
    else if (selected >= top + viewport) this.#rows.scrollTo(selected - viewport + 1);
    else this.#rows.scrollTo(top);
  }

  #viewportRows(): number {
    const height = this.#rows.height;
    if (typeof height === 'number' && height > 0) return height;
    // Before the first layout pass, estimate from the terminal.
    return Math.max(MIN_VIEWPORT_ROWS, this.renderer.terminalHeight - CHROME_ROWS);
  }

  #selectedIndex(): number {
    const log = this.#renderedState?.experimentLog;
    if (log === undefined || log === null || log.selectedId === null) return 0;
    return Math.max(0, log.entries.map(entryKey).indexOf(log.selectedId));
  }

  #line(content: string, fg: string): void {
    this.#rows.add(
      new TextRenderable(this.renderer, {
        content,
        fg,
        width: '100%',
        wrapMode: 'none',
        truncate: true,
      }),
    );
  }

  #bodyWidth(): number {
    const width = this.#availableWidth ?? this.renderer.terminalWidth;
    return Math.max(MIN_BODY_WIDTH, width - PANEL_CHROME_COLUMNS);
  }

  #clear(): void {
    for (const child of [...this.#rows.getChildren()]) {
      this.#rows.remove(child);
      child.destroyRecursively();
    }
  }
}

/** Widths of every column except the claim, which absorbs what is left over. */
const ID_WIDTH = 16;
const ROUNDS_WIDTH = 9;
const MEASURED_WIDTH = 12;
const VERDICT_WIDTH = 9;
const OUTCOME_WIDTH = 12;
const KEPT_WIDTH = 4;

export function resolveColumns(width: number): Columns {
  const claim = width >= CLAIM_MIN_WIDTH;
  const measured = width >= MEASURED_MIN_WIDTH;
  const kept = width >= KEPT_MIN_WIDTH;
  const fixed =
    ID_WIDTH +
    ROUNDS_WIDTH +
    OUTCOME_WIDTH +
    (measured ? MEASURED_WIDTH + VERDICT_WIDTH : 0) +
    (kept ? KEPT_WIDTH : 0);
  return {
    claim,
    measured,
    verdict: measured,
    kept,
    // Exactly the remaining width, so the row fills the panel without
    // overflowing it and losing the trailing columns to truncation.
    claimWidth: claim ? Math.max(20, width - fixed) : 0,
  };
}

export function headerRow(columns: Columns): string {
  const parts = [' Hypothesis'.padEnd(ID_WIDTH), 'Rounds'.padEnd(ROUNDS_WIDTH)];
  if (columns.claim) parts.push('Implementation Details'.padEnd(columns.claimWidth));
  if (columns.measured) parts.push('Measured'.padEnd(MEASURED_WIDTH));
  if (columns.verdict) parts.push('Verdict'.padEnd(VERDICT_WIDTH));
  parts.push('Outcome'.padEnd(OUTCOME_WIDTH));
  if (columns.kept) parts.push('Kept');
  return parts.join('');
}

/**
 * The row split into the segments the view colors independently. Widths are
 * baked in so the segments still line up as separate renderables.
 */
export interface EntryCells {
  leading: string;
  outcome: string;
  trailing: string;
}

export function entryCells(entry: HypothesisEntry, columns: Columns): EntryCells {
  const marker = entry.active === true ? '▸' : ' ';
  const leading = [
    `${marker}${truncate(entry.hypothesis_id, ID_WIDTH - 1)}`.padEnd(ID_WIDTH),
    formatRounds(entry).padEnd(ROUNDS_WIDTH),
  ];
  if (columns.claim) {
    leading.push(
      truncate(sentenceCase(entry.claim ?? entry.action ?? '—'), columns.claimWidth - 1).padEnd(
        columns.claimWidth,
      ),
    );
  }
  if (columns.measured) leading.push(formatMeasured(entry).padEnd(MEASURED_WIDTH));
  if (columns.verdict) {
    leading.push(sentenceCase(entry.judge_verdict ?? '—').padEnd(VERDICT_WIDTH));
  }
  return {
    leading: leading.join(''),
    outcome: truncate(sentenceCase(entry.resolved_outcome ?? 'active'), OUTCOME_WIDTH - 1).padEnd(
      OUTCOME_WIDTH,
    ),
    trailing: columns.kept ? (entry.kept === true ? 'Yes' : 'No') : '',
  };
}

export function entryRow(entry: HypothesisEntry, columns: Columns): string {
  const cells = entryCells(entry, columns);
  return `${cells.leading}${cells.outcome}${cells.trailing}`;
}

/**
 * Green for a hypothesis that held, red for one that did not, and the active
 * accent while it is still open. Outcomes with no such reading (``continue``,
 * ``inconclusive``) stay in body text rather than being forced into a verdict.
 */
export function outcomeColor(theme: Theme, entry: HypothesisEntry): string {
  const outcome = entry.resolved_outcome ?? null;
  if (entry.active === true || outcome === null) return theme.warning;
  if (outcome === 'proven') return theme.success;
  if (outcome === 'disproven' || outcome === 'rejected') return theme.error;
  return theme.textPrimary;
}

/** Capitalises a wire value for display without touching the rest of it. */
export function sentenceCase(value: string): string {
  const index = value.search(/[a-z]/i);
  if (index === -1) return value;
  return value.slice(0, index) + value.charAt(index).toUpperCase() + value.slice(index + 1);
}

export function formatRounds(entry: HypothesisEntry): string {
  return entry.first_round === entry.last_round
    ? String(entry.first_round)
    : `${entry.first_round}-${entry.last_round}`;
}

export function formatMeasured(entry: HypothesisEntry): string {
  const delta = entry.perf_delta_pct;
  if (typeof delta === 'number') {
    const sign = delta > 0 ? '+' : '';
    return `${sign}${delta.toFixed(delta >= 10 || delta <= -10 ? 0 : 1)}%`;
  }
  if (typeof entry.perf_metric === 'number') return trimNumber(entry.perf_metric);
  return '—';
}

function rowId(index: number): string {
  return `experiment-row-${index}`;
}

function truncate(value: string, width: number): string {
  if (width <= 1) return '';
  return value.length <= width ? value : `${value.slice(0, Math.max(1, width - 1))}…`;
}

function trimNumber(value: number): string {
  return Number.isInteger(value) ? String(value) : value.toFixed(2).replace(/\.?0+$/, '');
}
