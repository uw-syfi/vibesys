import {BoxRenderable, type CliRenderer, ScrollBoxRenderable, TextRenderable} from '@opentui/core';
import {formatFileChange} from '../design-log.js';
import {
  designRoundFor,
  detailedHypothesis,
  experimentIndexItems,
  hypothesisRoundNumbers,
  selectedExperimentIndexItem,
  unownedExperimentRounds,
} from '../experiments.js';
import type {SessionController} from '../session-controller.js';
import {
  type ExperimentEntry,
  type ExperimentIndexItem,
  experimentLogVisible,
  focusedPane,
  type HypothesisPlanningActivity,
  type SessionState,
} from '../session-model.js';
import type {Theme} from '../theme.js';
import {fillLayer} from './box-fill.js';
import {
  CLAIM_MIN_WIDTH,
  type Columns,
  entryCells,
  hypothesisMetadata,
  kickoffStages,
  MEASURED_MIN_WIDTH,
  outcomeColor,
  planLogRenderMode,
  planningHypothesisLabel,
  planningStageSummary,
  planTable,
  resolveColumns,
  roundMetadata,
  rowId,
  selectionCaret,
  unownedRoundRow,
} from './experiment-log-model.js';
import {applyPaneFocus, paneBorderColor, paneBorderStyle, paneTitle} from './focus.js';
import {elapsedLabel} from './previews.js';

const MIN_BODY_WIDTH = 40;
/**
 * Border, horizontal padding, and the scrollbar gutter. The gutter is reserved
 * even when the log fits, so rows do not reflow the moment it starts to scroll.
 */
const PANEL_CHROME_COLUMNS = 5;
/** App header, panel border, column header, footer, key help, and input box. */
const CHROME_ROWS = 10;
const MIN_VIEWPORT_ROWS = 3;

/**
 * Columns past the identity pair are dropped as the terminal narrows, widest
 * first, so hypothesis and rounds always survive.
 */
const EXPERIMENTS_TITLE = 'Experiments';

/**
 * Panel width at which the table still shows the claim, which is the row's
 * identity in words. Anything that takes columns from the table reads this
 * rather than restating the threshold.
 */
export const LOG_CLAIM_PANEL_WIDTH = CLAIM_MIN_WIDTH + PANEL_CHROME_COLUMNS;
/** Panel width that still carries the measured column. */
export const LOG_COMPACT_PANEL_WIDTH = MEASURED_MIN_WIDTH + PANEL_CHROME_COLUMNS;

export type {Columns} from './experiment-log-model.js';
export {
  entryCells,
  entryLeadingMarker,
  formatMeasured,
  formatRounds,
  headerRow,
  hypothesisMetadata,
  measuredDirection,
  outcomeColor,
  outcomeLabel,
  resolveColumns,
  selectionCaret,
  sentenceCase,
  unownedRoundCells,
  unownedRoundRow,
} from './experiment-log-model.js';

export class ExperimentLogView {
  readonly output: BoxRenderable;
  readonly #fill: BoxRenderable;
  readonly #header: TextRenderable;
  readonly #headerRule: TextRenderable;
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
    // (tui/conventions.md). Same call as `chat-pane`.
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
    // Meaningful only under the table's column header, so it costs no row in
    // every other view (kickoff, drill-down, loading, error, empty): `#clear`
    // collapses it to height 0 before every render, and only `#renderTable`
    // opens it back to 1. Inside the table view that height is fixed
    // regardless of how many rows follow, so it is a permanent row there
    // (tui/conventions.md, "nothing moves that does not have to") without
    // taxing views that have no header row to rule under.
    this.#headerRule = new TextRenderable(renderer, {
      content: '',
      fg: theme.border,
      width: '100%',
      height: 0,
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
    this.output.add(this.#headerRule);
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
    this.#headerRule.fg = theme.border;
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

    const mode = planLogRenderMode(state);
    if (mode.kind === 'detail') {
      this.#renderDetail(mode.entry, state);
      if (previousDetailKey !== state.hypothesisDetail?.entryKey) this.#rows.scrollTo(0);
      return;
    }
    if (mode.kind === 'error') {
      this.#header.content = '';
      this.#line(mode.message, this.#theme.conversation.failure.content);
      return;
    }
    if (mode.kind === 'loading') {
      this.#header.content = '';
      this.#line('Loading experiments...', this.#theme.textSubtle);
      return;
    }
    if (mode.kind === 'kickoff') {
      this.#renderKickoff(mode.activity, state);
      return;
    }
    if (mode.kind === 'table') {
      this.#renderTable(state);
      return;
    }
    this.#header.content = '';
    this.#line('No hypotheses have been recorded yet.', this.#theme.textSubtle);
    this.#footerLine.content = 'The first one appears once the orchestrator has planned a round.';
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
      const columns = resolveColumns(this.#bodyWidth());
      this.#line('UNASSOCIATED ROUNDS', this.#theme.textSubtle);
      for (const [index, roundNumber] of unowned.entries()) {
        this.#roundRow(roundNumber, columns, selectedUnownedRound === roundNumber, index + 1);
      }
    }
    this.#footerLine.content =
      unowned.length === 0
        ? 'The hypothesis list will appear here when planning completes.'
        : '↑↓: select activity or recorded round · Enter: open';
  }

  #renderTable(state: SessionState): void {
    const plan = planTable(state, this.#bodyWidth());
    if (plan === null) return;
    this.#header.content = plan.header;
    // A permanent rule under the header (tui/conventions.md, "nothing moves
    // that does not have to"): fixed at height 1 regardless of row count, so
    // it reads as a header rather than as one more line of text that happens
    // to sit above the rows.
    this.#headerRule.height = 1;
    this.#headerRule.content = '─'.repeat(this.#bodyWidth());
    for (const row of plan.rows) {
      if (row.kind === 'hypothesis') {
        this.#row(row.entry, row.key, plan.columns, row.selected, row.index);
      } else if (row.kind === 'round') {
        this.#roundRow(row.roundNumber, plan.columns, row.selected, row.index);
      } else if (row.kind === 'activity-heading') {
        this.#line('CURRENT ACTIVITY', this.#theme.textSubtle);
      } else {
        this.#renderActivity(row.item, row.existingHypotheses, row.selected);
      }
    }
    // Keyboard selection nudges the scroll position only when the row would
    // otherwise be off screen, so the wheel stays in charge the rest of the
    // time. Computed rather than deferred to scrollChildIntoView, which needs
    // a layout pass the freshly added rows have not had yet.
    this.#followSelection(plan.selectedRenderIndex, plan.renderedRows);
    this.#footerLine.content = plan.footer;
  }

  #renderDetail(entry: ExperimentEntry, state: SessionState): void {
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
      '↑↓: select round · Enter or click: open trajectory · d: diff · Esc: hypotheses';
  }

  /**
   * The selected round's file changes. Only the files: every stage fact for
   * the round is already on its row above, read from the same
   * `ExperimentRound`, so there is nothing here for the two to disagree
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
    entry: ExperimentEntry,
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

  #roundRow(
    roundNumber: number,
    columns: Columns,
    isSelected: boolean,
    navigationIndex: number,
  ): void {
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
    // One cell on the same column grid `entryCells` uses, not a single
    // unstructured string: the round has a real round number and genuinely
    // recorded agent turns, but no hypothesis, measurement, or outcome yet,
    // so it lands under the same headers a hypothesis row would.
    row.add(
      this.#cell(
        unownedRoundRow(roundNumber, columns, isSelected),
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
    this.#headerRule.height = 0;
    this.#headerRule.content = '';
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
