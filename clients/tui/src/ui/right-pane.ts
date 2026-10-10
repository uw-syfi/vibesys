import {BoxRenderable, type CliRenderer, ScrollBoxRenderable, TextRenderable} from '@opentui/core';
import {projectPerformance} from '@vibesys/core-state';
import {renderDesignSummary} from '../design-log.js';
import {designRoundViews} from '../experiments.js';
import {PLOT_WIDTH, renderPerformanceCurve} from '../performance-chart.js';
import {focusedPane, type RightPane, type SessionState} from '../session-model.js';
import type {Theme} from '../theme.js';
import {applyPaneFocus, paneBorderColor, paneBorderStyle, paneTitle} from './focus.js';

/**
 * Terminal width below which two panes would both be too narrow to read. The
 * widest thing the right pane shows is the performance chart, which needs the
 * plot plus its axis; the left pane needs enough room for a conversation card.
 * Under this the client stays single-pane and visualizations fall back to the
 * modal they used before the split existed.
 */
export const MIN_SPLIT_WIDTH = 100;

/**
 * Columns around the plot that aren't plot columns: an 8-char axis value
 * gutter plus 2 for the ' ┤' separator (10 columns of chart gutter), plus 1
 * column of border and 1 column of padding on each side of the pane (4
 * columns).
 */
const RIGHT_PANE_CHROME = 10 + 4;
/**
 * Chart plot width plus its gutter, border, and padding. Derived from the
 * chart's own PLOT_WIDTH so the two cannot drift apart.
 */
const RIGHT_PANE_MIN = PLOT_WIDTH + RIGHT_PANE_CHROME;
const RIGHT_PANE_MAX = 84;
const RIGHT_PANE_SHARE = 0.45;
/** Columns the transcript needs to stay worth reading beside the pane. */
const LEFT_PANE_MIN = 38;

/** English presentation for semantic pane kinds stays in the TUI renderer. */
export function rightPaneTitle(pane: RightPane): string {
  return pane.view === 'perf' ? 'Performance' : 'Design changes';
}

/** Render one structured pane projection, or null until its first response lands. */
export function rightPaneContent(state: SessionState, pane: RightPane): string | null {
  if (pane.view === 'perf') {
    return pane.data === null
      ? null
      : renderPerformanceCurve(
          projectPerformance({
            ...pane.data,
            experiments: state.experimentLog?.entries,
            objectiveDirection: pane.context?.objective_direction,
          }),
          pane.context ?? undefined,
        );
  }
  if (!pane.available) return 'The design log is not available until a run is attached.';
  return state.designLog === null
    ? null
    : renderDesignSummary(designRoundViews(state.designLog, state.experimentLog?.entries ?? []));
}

/** True when the terminal has room for the transcript and a visualization. */
export function splitFits(terminalWidth: number): boolean {
  return terminalWidth >= MIN_SPLIT_WIDTH;
}

/**
 * Width in columns for the right pane. Derived from the terminal rather than a
 * fixed percentage, so a wide terminal gives the visualization real room while
 * the transcript keeps a readable floor.
 */
export function rightPaneWidth(terminalWidth: number): number {
  const share = Math.round(terminalWidth * RIGHT_PANE_SHARE);
  const capped = Math.min(RIGHT_PANE_MAX, Math.max(RIGHT_PANE_MIN, share));
  return Math.min(capped, terminalWidth - LEFT_PANE_MIN);
}

export class RightPaneView {
  readonly output: BoxRenderable;
  readonly #scroll: ScrollBoxRenderable;
  #theme: Theme;
  #renderedPane: RightPane | null = null;
  #renderedExperiments: SessionState['experimentLog'] = null;
  #renderedDesignLog: SessionState['designLog'] = null;

  constructor(
    private readonly renderer: CliRenderer,
    theme: Theme,
    onFocusRequest: () => void,
  ) {
    this.#theme = theme;
    this.output = new BoxRenderable(renderer, {
      id: 'right-pane',
      height: '100%',
      flexShrink: 0,
      flexDirection: 'column',
      paddingLeft: 1,
      paddingRight: 1,
      border: true,
      borderStyle: paneBorderStyle(false),
      borderColor: paneBorderColor(theme, false),
      title: paneTitle('Pane', false),
      visible: false,
      onMouseUp: onFocusRequest,
    });
    this.#scroll = new ScrollBoxRenderable(renderer, {
      id: 'right-pane-scroll',
      width: '100%',
      flexGrow: 1,
      stickyScroll: false,
      viewportCulling: true,
      verticalScrollbarOptions: {showArrows: false},
      onMouseUp: onFocusRequest,
    });
    this.output.add(this.#scroll);
  }

  applyTheme(theme: Theme): void {
    this.#theme = theme;
    this.#renderedPane = null;
    this.#renderedExperiments = null;
    this.#renderedDesignLog = null;
  }

  /** Scrolled by Page Up/Page Down while this pane holds focus. */
  scrollBy(delta: number): void {
    this.#scroll.scrollBy(delta, 'viewport');
  }

  render(
    state: SessionState,
    visible: boolean,
    width = rightPaneWidth(this.renderer.terminalWidth),
  ): void {
    const right = visible ? state.layout.right : null;
    if (right === null) {
      this.output.visible = false;
      return;
    }
    this.output.visible = true;
    this.output.width = width;
    // The focused pane is the one that takes keys, and says so in its title
    // marker, its frame, and its border colour. Every pane asks `focusedPane`,
    // so exactly one of them can answer yes.
    const focused = focusedPane(state) === 'performance';
    applyPaneFocus(this.output, this.#theme, rightPaneTitle(right), focused);
    if (
      right === this.#renderedPane &&
      state.experimentLog === this.#renderedExperiments &&
      state.designLog === this.#renderedDesignLog
    ) {
      return;
    }
    this.#renderedPane = right;
    this.#renderedExperiments = state.experimentLog;
    this.#renderedDesignLog = state.designLog;
    this.#clear();

    if (right.error !== null) {
      this.#line(right.error, this.#theme.conversation.failure.content);
      return;
    }
    const content = rightPaneContent(state, right);
    if (right.pending && content === null) {
      this.#line('Loading...', this.#theme.textSubtle);
      return;
    }
    for (const line of (content ?? '').split('\n')) {
      this.#line(line, this.#theme.textPrimary);
    }
    this.#scroll.scrollTo(0);
  }

  #line(content: string, fg: string): void {
    this.#scroll.add(
      new TextRenderable(this.renderer, {
        content,
        fg,
        width: '100%',
        flexShrink: 0,
        // Chart rows are drawn to fit the pane, but a summary line can run
        // past it. Wrapping keeps the whole number readable; truncating it
        // mid-value would be worse than a second row.
        wrapMode: 'word',
      }),
    );
  }

  #clear(): void {
    for (const child of [...this.#scroll.getChildren()]) {
      this.#scroll.remove(child);
      child.destroyRecursively();
    }
  }
}
