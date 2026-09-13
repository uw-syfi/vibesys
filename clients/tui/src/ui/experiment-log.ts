import {BoxRenderable, type CliRenderer, ScrollBoxRenderable, TextRenderable} from '@opentui/core';
import type {HypothesisEntry, HypothesisRound} from '@vibesys/backend-client';
import type {SessionController} from '../session-controller.js';
import {
  designRoundFor,
  detailedHypothesis,
  type ExperimentIndexItem,
  experimentIndexItems,
  experimentLogVisible,
  focusedPane,
  type HypothesisPlanningActivity,
  hypothesisPlanningActivity,
  hypothesisRoundNumbers,
  type SessionState,
  selectedExperimentIndexItem,
  unownedExperimentRounds,
} from '../session-model.js';
import {fillLayer} from './box-fill.js';
import {formatFileChange} from './design-log.js';
import {applyPaneFocus, paneBorderColor, paneBorderStyle, paneTitle} from './focus.js';
import {elapsedLabel} from './previews.js';
import {displayWidth, padToWidth, truncateToWidth} from './text-width.js';
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
const EXPERIMENTS_TITLE = 'Experiments';

/**
 * Panel width at which the table still shows the claim, which is the row's
 * identity in words. Anything that takes columns from the table reads this
 * rather than restating the threshold.
 */
export const LOG_CLAIM_PANEL_WIDTH = CLAIM_MIN_WIDTH + PANEL_CHROME_COLUMNS;
/** Panel width that still carries the measured column. */
export const LOG_COMPACT_PANEL_WIDTH = MEASURED_MIN_WIDTH + PANEL_CHROME_COLUMNS;

interface Columns {
  claim: boolean;
  measured: boolean;
  kept: boolean;
  claimWidth: number;
}

export class ExperimentLogView {
  readonly output: BoxRenderable;
  readonly #fill: BoxRenderable;
  readonly #header: TextRenderable;
  readonly #rows: ScrollBoxRenderable;
  readonly #footerLine: TextRenderable;
  #theme: Theme;
  #renderedState: SessionState | null = null;
  #renderedWidth = 0;
  #availableWidth: number | null = null;
  #elapsedTimer: ReturnType<typeof setInterval> | null = null;
  #activeActivityLine: {text: TextRenderable; content: string; startedAt: string} | null = null;

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
      borderStyle: paneBorderStyle(false),
      borderColor: paneBorderColor(theme, false),
      visible: false,
      title: paneTitle(EXPERIMENTS_TITLE, false),
      onMouseUp: () => this.controller.focusPane('left'),
    });
    // The pane surface, on its own layer so the rounded frame stays rounded
    // (tui-conventions.md). Same call as `chat-pane`.
    this.#fill = fillLayer(this.output, 'experiment-log-fill', theme.canvas);
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
      onMouseUp: () => this.controller.focusPane('left'),
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
    this.output.borderColor = theme.border;
    this.#fill.backgroundColor = theme.canvas;
    this.#header.fg = theme.textSubtle;
    this.#footerLine.fg = theme.textSubtle;
    this.#renderedState = null;
  }

  destroy(): void {
    this.#stopElapsedTimer();
  }

  render(state: SessionState): void {
    const log = experimentLogVisible(state) ? state.experimentLog : null;
    if (log === null) {
      this.output.visible = false;
      this.#renderedState = null;
      return;
    }
    this.output.visible = true;
    // Both the label and the focus channels are derived from the state being
    // rendered, so this runs on every notification, including the ones the
    // cache below returns from. Naming the panel `Experiments` here and
    // correcting it during detail rendering left a same-state notification
    // titling a hypothesis body with the index's title.
    const detail = detailedHypothesis(state);
    applyPaneFocus(
      this.output,
      this.#theme,
      detail === null ? EXPERIMENTS_TITLE : `Hypothesis ${detail.hypothesis_id}`,
      focusedPane(state) === 'experiments',
    );
    const width = this.#availableWidth ?? this.renderer.terminalWidth;
    if (state === this.#renderedState && width === this.#renderedWidth) return;
    const previousDetailKey = this.#renderedState?.hypothesisDetail?.entryKey ?? null;
    this.#renderedState = state;
    this.#renderedWidth = width;
    this.#clear();

    if (detail !== null) {
      this.#renderDetail(detail, state);
      if (previousDetailKey !== state.hypothesisDetail?.entryKey) this.#rows.scrollTo(0);
      return;
    }

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
    const activity = hypothesisPlanningActivity(state);
    if (log.entries.length === 0) {
      if (activity !== null && unownedExperimentRounds(state).length === 0) {
        this.#renderKickoff(activity, state);
        return;
      }
      if (unownedExperimentRounds(state).length > 0) {
        this.#renderTable(state);
        return;
      }
      this.#header.content = '';
      this.#line('No hypotheses have been recorded yet.', this.#theme.textSubtle);
      this.#footerLine.content = 'The first one appears once the orchestrator has planned a round.';
      return;
    }
    this.#renderTable(state);
  }

  #renderKickoff(activity: HypothesisPlanningActivity, state: SessionState): void {
    const hypothesis = planningHypothesisLabel(0);
    const selectedUnownedRound = state.experimentLog?.selectedUnownedRound ?? null;
    const activitySelected = selectedUnownedRound === null;
    this.#header.content = `Planning ${hypothesis} · Round ${activity.roundNumber}`;
    this.#line('Run kickoff', this.#theme.textSubtle);
    for (const stage of kickoffStages(activity.stage, hypothesis)) {
      if (stage.current) {
        this.#activityLine(`${stage.marker} ${stage.label}`, activity, activitySelected);
      } else this.#line(`${stage.marker} ${stage.label}`, this.#theme.textSubtle);
    }
    this.#line(
      'This activity becomes the first hypothesis when the orchestrator finishes its plan.',
      this.#theme.textPrimary,
    );
    const unowned = unownedExperimentRounds(state);
    if (unowned.length > 0) {
      this.#line('UNASSOCIATED ROUNDS', this.#theme.textSubtle);
      for (const [index, roundNumber] of unowned.entries()) {
        this.#roundRow(roundNumber, selectedUnownedRound === roundNumber, index + 1);
      }
    }
    this.#footerLine.content =
      unowned.length === 0
        ? 'The hypothesis list will appear here when planning completes.'
        : '↑↓: select activity or recorded round · Enter: open';
  }

  #renderTable(state: SessionState): void {
    const log = state.experimentLog;
    if (log === null) return;
    const columns = resolveColumns(this.#bodyWidth());
    const items = experimentIndexItems(state);
    const selected = selectedExperimentIndexItem(state);
    this.#header.content = headerRow(columns, measuredDirection(log.entries));
    let selectedRenderIndex = 0;
    let renderedRows = 0;
    for (const [navigationIndex, item] of items.entries()) {
      const isSelected = item.key === selected?.key;
      if (item.kind === 'hypothesis') {
        const entryIndex = log.entries.indexOf(item.entry);
        this.#row(item.entry, item.key, columns, isSelected, entryIndex);
        if (isSelected) selectedRenderIndex = renderedRows;
        renderedRows += 1;
      } else if (item.kind === 'round') {
        this.#roundRow(item.roundNumber, isSelected, navigationIndex);
        if (isSelected) selectedRenderIndex = renderedRows;
        renderedRows += 1;
      } else {
        this.#line('CURRENT ACTIVITY', this.#theme.textSubtle);
        renderedRows += 1;
        this.#renderActivity(item, log.entries.length, isSelected);
        if (isSelected) selectedRenderIndex = renderedRows;
        renderedRows += 1;
      }
    }
    // Keyboard selection nudges the scroll position only when the row would
    // otherwise be off screen, so the wheel stays in charge the rest of the
    // time. Computed rather than deferred to scrollChildIntoView, which needs
    // a layout pass the freshly added rows have not had yet.
    this.#followSelection(selectedRenderIndex, renderedRows);
    const selectedNavigationIndex =
      selected === null ? 0 : items.findIndex(item => item.key === selected.key);
    const position = `${Math.max(0, selectedNavigationIndex) + 1}/${items.length}`;
    // The hint is the first thing to give up room; truncating it mid-word
    // reads as breakage rather than as a narrow terminal.
    const hint =
      this.#bodyWidth() >= HINT_MIN_WIDTH
        ? '↑↓ or scroll: select · Enter or click: open hypothesis'
        : '↑↓ · Enter';
    this.#footerLine.content = `${position} · ${hint}`;
  }

  #renderDetail(entry: HypothesisEntry, state: SessionState): void {
    const selectedRound = state.hypothesisDetail?.selectedRound ?? null;
    // The frame, its colour, and the title are `render`'s: it is the one place
    // that runs on every notification, so it is the one place that can keep
    // them in step with what the panel is showing.
    this.#header.content = hypothesisMetadata(entry);
    const title = entry.title?.trim();
    if (title) this.#line(title, this.#theme.textStrong);
    this.#line('HYPOTHESIS', this.#theme.textSubtle);
    this.#wrappedLine(
      entry.claim?.trim() || 'No hypothesis text was recorded.',
      this.#theme.textPrimary,
    );
    this.#line('', this.#theme.textPrimary);
    this.#line('ROUNDS', this.#theme.textSubtle);
    const rounds = hypothesisRoundNumbers(entry);
    if (rounds.length === 0) {
      this.#line('No recorded rounds.', this.#theme.textSubtle);
    } else {
      for (const roundNumber of rounds) {
        const round = entry.rounds?.find(candidate => candidate.round === roundNumber);
        const selected = roundNumber === selectedRound;
        const row = new BoxRenderable(this.renderer, {
          id: `hypothesis-round-${roundNumber}`,
          width: '100%',
          height: 1,
          flexShrink: 0,
          ...(selected ? {backgroundColor: this.#theme.selectedSurface} : {}),
          onMouseUp: () => this.controller.openRound(roundNumber),
        });
        row.add(
          this.#cell(
            `${selected ? '›' : ' '} ${roundMetadata(roundNumber, round)}`,
            this.#theme.textPrimary,
            selected,
          ),
        );
        this.#rows.add(row);
      }
    }
    this.#renderRoundDesign(state, selectedRound);
    this.#footerLine.content =
      '↑↓: select round · Enter or click: open trajectory · Esc: hypotheses';
  }

  /**
   * The selected round's file changes. Only the files: every stage fact for
   * the round is already on its row above, read from the same
   * `HypothesisRound`, so there is nothing here for the two to disagree
   * about. Absent entirely until the design log has loaded, so the drill-down
   * never shows a placeholder it cannot yet explain.
   */
  #renderRoundDesign(state: SessionState, selectedRound: number | null): void {
    const design = designRoundFor(state, selectedRound);
    if (design === null) return;
    this.#line('', this.#theme.textPrimary);
    this.#line(`ROUND ${design.round} CHANGES`, this.#theme.textSubtle);
    const files = design.files ?? null;
    if (files === null) {
      this.#line('File changes are not recorded for this round.', this.#theme.textSubtle);
      return;
    }
    if (files.length === 0) {
      this.#line('No workspace files changed.', this.#theme.textSubtle);
      return;
    }
    for (const file of files) {
      const color =
        file.change === 'added'
          ? this.#theme.success
          : file.change === 'deleted'
            ? this.#theme.error
            : this.#theme.textPrimary;
      this.#line(formatFileChange(file), color);
    }
  }

  #renderActivity(
    item: Extract<ExperimentIndexItem, {kind: 'activity'}>,
    existingHypotheses: number,
    selected: boolean,
  ): void {
    const {activity} = item;
    const hypothesis = planningHypothesisLabel(existingHypotheses);
    this.#activityLine(
      `● Planning ${hypothesis} · ${planningStageSummary(activity.stage)} · Round ${activity.roundNumber}`,
      activity,
      selected,
    );
  }

  #row(
    entry: HypothesisEntry,
    entryKey: string,
    columns: Columns,
    isSelected: boolean,
    index: number,
  ): void {
    const cells = entryCells(entry, columns, isSelected);
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
        this.controller.focusPane('left');
        this.controller.openHypothesisDetail(entryKey);
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

  #roundRow(roundNumber: number, isSelected: boolean, navigationIndex: number): void {
    const row = new BoxRenderable(this.renderer, {
      id: `unowned-round-${roundNumber}`,
      width: '100%',
      height: 1,
      flexShrink: 0,
      ...(isSelected ? {backgroundColor: this.#theme.selectedSurface} : {}),
      onMouseUp: () => {
        this.controller.focusPane('left');
        this.controller.moveExperimentSelection(navigationIndex - this.#selectedNavigationIndex());
      },
    });
    row.add(
      this.#cell(
        `${selectionCaret(isSelected)} Round ${roundNumber} · recorded agent turns · no hypothesis`,
        this.#theme.textPrimary,
        isSelected,
      ),
    );
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

  #selectedNavigationIndex(): number {
    const state = this.#renderedState;
    if (state === null) return 0;
    const selected = selectedExperimentIndexItem(state);
    const index =
      selected === null
        ? 0
        : experimentIndexItems(state).findIndex(item => item.key === selected.key);
    return Math.max(0, index);
  }

  #activityLine(content: string, activity: HypothesisPlanningActivity, selected: boolean): void {
    const row = new BoxRenderable(this.renderer, {
      id: 'planning-activity',
      width: '100%',
      height: 1,
      flexShrink: 0,
      ...(selected ? {backgroundColor: this.#theme.selectedSurface} : {}),
      onMouseUp: () => {
        this.controller.focusPane('left');
        this.controller.selectExperimentActivity();
      },
    });
    const prefixed = `${selectionCaret(selected)} ${content}`;
    const text = new TextRenderable(this.renderer, {
      content: prefixed,
      fg: this.#theme.warning,
      width: '100%',
      ...(selected ? {bg: this.#theme.selectedSurface} : {}),
      wrapMode: 'none',
      truncate: true,
    });
    row.add(text);
    this.#rows.add(row);
    if (activity.startedAt === undefined || !Number.isFinite(Date.parse(activity.startedAt)))
      return;
    this.#activeActivityLine = {text, content: prefixed, startedAt: activity.startedAt};
    this.#refreshElapsedActivity();
    this.#syncElapsedTimer();
  }

  #line(content: string, fg: string): TextRenderable {
    const text = new TextRenderable(this.renderer, {
      content,
      fg,
      width: '100%',
      wrapMode: 'none',
      truncate: true,
    });
    this.#rows.add(text);
    return text;
  }

  #wrappedLine(content: string, fg: string): TextRenderable {
    const text = new TextRenderable(this.renderer, {
      content,
      fg,
      width: '100%',
      flexShrink: 0,
      wrapMode: 'word',
    });
    this.#rows.add(text);
    return text;
  }

  /** Scroll hypothesis prose without moving the round selection. */
  scrollBy(delta: number): void {
    this.#rows.scrollBy(delta, 'viewport');
  }

  #bodyWidth(): number {
    const width = this.#availableWidth ?? this.renderer.terminalWidth;
    return Math.max(MIN_BODY_WIDTH, width - PANEL_CHROME_COLUMNS);
  }

  #clear(): void {
    this.#activeActivityLine = null;
    this.#stopElapsedTimer();
    for (const child of [...this.#rows.getChildren()]) {
      this.#rows.remove(child);
      child.destroyRecursively();
    }
  }

  #syncElapsedTimer(): void {
    if (this.#activeActivityLine === null || this.#elapsedTimer !== null) return;
    this.#elapsedTimer = setInterval(() => this.#refreshElapsedActivity(), 1000);
  }

  #refreshElapsedActivity(): void {
    const activity = this.#activeActivityLine;
    if (activity === null) return;
    const elapsed = Date.now() - Date.parse(activity.startedAt);
    if (!Number.isFinite(elapsed)) return;
    activity.text.content = `${activity.content} · ${elapsedLabel(elapsed)}`;
  }

  #stopElapsedTimer(): void {
    if (this.#elapsedTimer === null) return;
    clearInterval(this.#elapsedTimer);
    this.#elapsedTimer = null;
  }
}

/** Widths of every column except the claim, which absorbs what is left over. */
const ID_WIDTH = 15;
const ROUNDS_WIDTH = 8;
// Room for a trimmed value plus a short unit ("55434.2 ops/s"). A longer unit
// truncates here and reads in full from the hypothesis drill-down. Growing
// this further would push the fixed row past MEASURED_MIN_WIDTH.
const MEASURED_WIDTH = 20;
const OUTCOME_WIDTH = 11;
const KEPT_WIDTH = 4;
const COLUMN_GAP = '  ';

export function resolveColumns(width: number): Columns {
  const claim = width >= CLAIM_MIN_WIDTH;
  const measured = width >= MEASURED_MIN_WIDTH;
  const kept = width >= KEPT_MIN_WIDTH;
  const fixed =
    ID_WIDTH +
    ROUNDS_WIDTH +
    OUTCOME_WIDTH +
    (measured ? MEASURED_WIDTH : 0) +
    (kept ? KEPT_WIDTH : 0);
  const visibleColumns = 3 + (claim ? 1 : 0) + (measured ? 1 : 0) + (kept ? 1 : 0);
  const gutterWidth = (visibleColumns - 1) * COLUMN_GAP.length;
  return {
    claim,
    measured,
    kept,
    // Exactly the remaining width, so the row fills the panel without
    // overflowing it and losing the trailing columns to truncation.
    claimWidth: claim ? Math.max(20, width - fixed - gutterWidth) : 0,
  };
}

export function headerRow(columns: Columns, direction: 'max' | 'min' | null = null): string {
  const parts = [padToWidth(' Hypothesis', ID_WIDTH), padToWidth('Rounds', ROUNDS_WIDTH)];
  if (columns.claim) parts.push(padToWidth('Implementation Details', columns.claimWidth));
  if (columns.measured) {
    // The glyph is the way improvement points, so a signed delta below reads
    // as good or bad without task knowledge. A glyph rather than color, which
    // the outcome column already spends; the drill-down spells out the word.
    const label = direction === null ? 'Measured' : `Measured ${direction === 'max' ? '↑' : '↓'}`;
    parts.push(padToWidth(label, MEASURED_WIDTH));
  }
  parts.push(padToWidth('Outcome', OUTCOME_WIDTH));
  if (columns.kept) parts.push(padToWidth('Kept', KEPT_WIDTH));
  return parts.join(COLUMN_GAP);
}

/**
 * The selection caret shared by every selectable row in the log: `'›'` for the
 * selected row, a matching blank otherwise, so a row's other columns land in
 * the same place whether or not it is selected. Independent of any
 * status/active glyph the row also carries, following the precedent in
 * theme-picker.ts and the hypothesis drill-down (`#renderDetail`), where the
 * background swap alone was not enough to read selection on a low-contrast
 * terminal.
 */
export function selectionCaret(isSelected: boolean): string {
  return isSelected ? '›' : ' ';
}

/**
 * The two-character leading marker on a hypothesis row: the selection caret,
 * then the active-hypothesis marker. The two are independent signals, so both
 * render in the same row without either overwriting the other.
 */
export function entryLeadingMarker(entry: HypothesisEntry, isSelected: boolean): string {
  const active = entry.active === true ? '▸' : ' ';
  return `${selectionCaret(isSelected)}${active}`;
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

export function entryCells(
  entry: HypothesisEntry,
  columns: Columns,
  isSelected = false,
): EntryCells {
  const marker = entryLeadingMarker(entry, isSelected);
  const leading = [
    fitColumn(
      `${marker}${truncate(entry.hypothesis_id, ID_WIDTH - displayWidth(marker))}`,
      ID_WIDTH,
    ),
    fitColumn(formatRounds(entry), ROUNDS_WIDTH),
  ];
  if (columns.claim) {
    leading.push(
      fitColumn(
        sentenceCase(entry.title ?? entry.claim ?? entry.action ?? '—'),
        columns.claimWidth,
      ),
    );
  }
  if (columns.measured) leading.push(fitColumn(formatMeasured(entry), MEASURED_WIDTH));
  return {
    leading: leading.join(COLUMN_GAP),
    // These are separate renderables so outcome can carry semantic color.
    // Put gutters on the following segment rather than relying on trailing
    // padding surviving across renderable boundaries.
    outcome: `${COLUMN_GAP}${fitColumn(outcomeLabel(entry), OUTCOME_WIDTH)}`,
    trailing: columns.kept
      ? `${COLUMN_GAP}${fitColumn(
          entry.kept === true ? 'Yes' : entry.kept === false ? 'No' : '—',
          KEPT_WIDTH,
        )}`
      : '',
  };
}

/** Exactly `width` cells: truncated if over, space-padded if under. */
function fitColumn(value: string, width: number): string {
  return padToWidth(truncate(value, width), width);
}

export function entryRow(entry: HypothesisEntry, columns: Columns, isSelected = false): string {
  const cells = entryCells(entry, columns, isSelected);
  return `${cells.leading}${cells.outcome}${cells.trailing}`;
}

/**
 * Green for a hypothesis that held, red for one that did not, and the active
 * accent while it is still open. Outcomes with no such reading stay in body
 * text rather than being forced into a verdict: `inconclusive`, where a
 * trusted measurement did not decide the claim, and `unmeasured`, where the
 * framework measured nothing to decide it with.
 */
export function outcomeColor(theme: Theme, entry: HypothesisEntry): string {
  const outcome = entry.resolved_outcome ?? null;
  if (entry.active === true) return theme.warning;
  if (outcome === null) return theme.textPrimary;
  if (outcome === 'proven') return theme.success;
  if (outcome === 'disproven' || outcome === 'rejected') return theme.error;
  return theme.textPrimary;
}

/** Map backend resolution terms to concise operator-facing hypothesis decisions. */
export function outcomeLabel(entry: HypothesisEntry): string {
  if (entry.active === true) return 'Active';
  if (entry.resolved_outcome === 'proven') return 'Accepted';
  if (entry.resolved_outcome === 'disproven') return 'Rejected';
  return sentenceCase(entry.resolved_outcome ?? '—');
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

/**
 * Delta wins when present. `not_framework_measured` is checked next, before
 * the metric fallback, because the entry-level `perf_metric` is null in that
 * case anyway. A `baseline_unresolved` absolute value gets a `? ` prefix, not
 * a suffix, so the MEASURED_WIDTH truncation in `fitColumn` can never cut it
 * off. `no_baseline_yet` and a legacy entry with no reason keep the bare
 * value: a first measurement is itself a deliberate absolute display.
 */
export function formatMeasured(entry: HypothesisEntry): string {
  const delta = entry.perf_delta_pct;
  if (typeof delta === 'number') return formatDelta(delta);
  if (entry.perf_delta_reason === 'not_framework_measured') return 'self-reported';
  if (typeof entry.perf_metric === 'number') {
    const marker = entry.perf_delta_reason === 'baseline_unresolved' ? '? ' : '';
    return `${marker}${trimNumber(entry.perf_metric)}${entry.perf_unit ? ` ${entry.perf_unit}` : ''}`;
  }
  return '—';
}

function formatDelta(delta: number): string {
  const sign = delta > 0 ? '+' : '';
  return `${sign}${delta.toFixed(delta >= 10 || delta <= -10 ? 0 : 1)}%`;
}

/**
 * The one improvement direction the header can honestly carry. Null when no
 * entry recorded a direction, or when entries disagree, where a single glyph
 * would mislabel some rows.
 */
export function measuredDirection(entries: readonly HypothesisEntry[]): 'max' | 'min' | null {
  let direction: 'max' | 'min' | null = null;
  for (const entry of entries) {
    const candidate = entry.perf_direction ?? null;
    if (candidate === null) continue;
    if (direction === null) direction = candidate;
    else if (direction !== candidate) return null;
  }
  return direction;
}

export function hypothesisMetadata(entry: HypothesisEntry): string {
  const parts = [`Rounds ${formatRounds(entry)}`];
  if (entry.judge_verdict !== null && entry.judge_verdict !== undefined) {
    parts.push(`Judge ${sentenceCase(entry.judge_verdict)}`);
  }
  parts.push(`Decision ${outcomeLabel(entry)}`);
  if (entry.kept === true) parts.push('Candidate kept');
  else if (entry.kept === false) parts.push('Candidate reverted');
  parts.push(...measurementMetadata(entry));
  return parts.join(' · ');
}

/**
 * The measurement spelled out where width is unbounded: metric identity and
 * direction as words, then the absolute value, its baseline, and the causal
 * delta the table compresses into one cell.
 */
function measurementMetadata(entry: HypothesisEntry): string[] {
  const parts: string[] = [];
  const name = entry.perf_metric_name ?? null;
  const direction =
    entry.perf_direction === 'max'
      ? 'maximize'
      : entry.perf_direction === 'min'
        ? 'minimize'
        : null;
  if (name !== null) parts.push(`Metric ${name}${direction === null ? '' : ` (${direction})`}`);
  else if (direction !== null) parts.push(`Direction ${direction}`);
  // Legacy rounds recorded the metric name as the unit; once the name clause
  // carries that identity, repeating it after each number is noise.
  const unit = entry.perf_unit && entry.perf_unit !== name ? ` ${entry.perf_unit}` : '';
  if (typeof entry.perf_metric === 'number') {
    parts.push(`Measured ${trimNumber(entry.perf_metric)}${unit}`);
  }
  if (typeof entry.perf_baseline_value === 'number') {
    parts.push(`Baseline ${trimNumber(entry.perf_baseline_value)}${unit}`);
  }
  if (typeof entry.perf_baseline_round === 'number') {
    parts.push(`Baseline round ${entry.perf_baseline_round}`);
  }
  if (entry.perf_baseline_commit) {
    parts.push(`Baseline commit ${entry.perf_baseline_commit.slice(0, 7)}`);
  }
  if (typeof entry.perf_delta_pct === 'number') {
    parts.push(`Delta ${formatDelta(entry.perf_delta_pct)}`);
  }
  const reason = deltaReasonLabel(entry.perf_delta_reason);
  if (reason !== null) parts.push(reason);
  return parts;
}

/**
 * Spells out why `perf_delta_pct` is absent. Exhaustive over the wire union
 * with no default case, so a reason value the client does not yet know how
 * to word fails the build instead of silently rendering nothing.
 */
function deltaReasonLabel(reason: HypothesisEntry['perf_delta_reason']): string | null {
  switch (reason) {
    case 'no_baseline_yet':
      return 'No baseline existed yet';
    case 'baseline_unresolved':
      return 'No trusted baseline resolved';
    case 'not_framework_measured':
      return 'Self-reported, not framework-measured';
    case null:
    case undefined:
      return null;
  }
}

function roundMetadata(roundNumber: number, round: HypothesisRound | undefined): string {
  const parts = [`Round ${roundNumber}`];
  if (round !== undefined) parts.push(`Judge ${judgeLabel(round)}`);
  if (typeof round?.perf_metric === 'number') {
    parts.push(`${trimNumber(round.perf_metric)}${round.perf_unit ? ` ${round.perf_unit}` : ''}`);
  }
  return parts.join(' · ');
}

/**
 * The round's own review state. `judge_verdict` is authoritative; a record
 * written before the framework stored one carries only `reviewed`, and
 * `passed` is the closest thing it has to a verdict.
 */
function judgeLabel(round: HypothesisRound): string {
  if (round.judge_verdict) return round.judge_verdict;
  return round.reviewed ? (round.passed ? 'pass' : 'fail') : 'pending';
}

function planningHypothesisLabel(existingHypotheses: number): string {
  return `Hypothesis ${existingHypotheses + 1}`;
}

function kickoffStages(
  stage: HypothesisPlanningActivity['stage'],
  hypothesis: string,
): Array<{marker: string; label: string; current: boolean}> {
  const current = (target: HypothesisPlanningActivity['stage']): boolean => stage === target;
  const completed = (target: HypothesisPlanningActivity['stage']): boolean =>
    (stage === 'profile' || stage === 'plan') && target === 'pre';
  return [
    {
      marker: current('pre') ? '●' : completed('pre') ? '✓' : '○',
      label: 'Decide whether profiling is needed',
      current: current('pre'),
    },
    {
      marker: current('profile') ? '●' : '○',
      label: current('profile') ? `Profile before ${hypothesis}` : 'Profile if needed',
      current: current('profile'),
    },
    {
      marker: current('plan') ? '●' : '○',
      label: `Form ${hypothesis}`,
      current: current('plan'),
    },
  ];
}

function planningStageSummary(stage: HypothesisPlanningActivity['stage']): string {
  const labels: Record<HypothesisPlanningActivity['stage'], string> = {
    pre: 'deciding whether profiling is needed',
    profile: 'profiling',
    plan: 'forming it',
  };
  return labels[stage];
}

function rowId(index: number): string {
  return `experiment-row-${index}`;
}

/**
 * At most `width` cells, ellipsized. Measured in cells, not code units: a CJK
 * value that fits its column by `String.length` can still be twice as wide on
 * screen, and slicing by code units can land inside a wide character.
 */
function truncate(value: string, width: number): string {
  if (width <= 1) return '';
  if (displayWidth(value) <= width) return value;
  return `${truncateToWidth(value, width - 1)}…`;
}

function trimNumber(value: number): string {
  return Number.isInteger(value) ? String(value) : value.toFixed(2).replace(/\.?0+$/, '');
}
