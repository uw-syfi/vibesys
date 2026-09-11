import {BoxRenderable, type CliRenderer, TextRenderable} from '@opentui/core';
import {
  type AgentPhase,
  hasActiveAgentTiming,
  type RoundSummary,
  roundAgentElapsedMs,
} from '@vibesys/core-state';
import type {SessionController} from '../session-controller.js';
import type {SessionState} from '../session-model.js';
import {
  focusedPane,
  scopedRounds,
  stripRounds,
  visiblePhases,
  visibleRoundNumber,
} from '../session-model.js';
import {
  type AgentGraph,
  type EdgeTone,
  graphPaneBounds,
  graphWindow,
  layoutAgentGraph,
  NODE_HEIGHT,
} from './agent-graph.js';
import {agentRuntimeLabel} from './agent-runtime-label.js';
import {fillLayer} from './box-fill.js';
import {applyPaneFocus, paneBorderColor, paneBorderStyle, paneTitle} from './focus.js';
import {elapsedLabel} from './previews.js';
import type {Theme} from './theme.js';

const STATUS_MARKER: Record<AgentPhase['status'], string> = {
  pending: '○',
  active: '●',
  completed: '✓',
  failed: '×',
  cancelled: '■',
  interrupted: '!',
};

/** Width the stacked fallback uses, and the width this pane had before. */
export const STACKED_WIDTH = 30;
/** Border top and bottom; the title rides the top border. */
const PANE_VCHROME = 2;
/** The heading row above the graph, which is drawn whenever phases exist. */
const HEADING_ROWS = 1;
/** Columns the transcript needs to stay worth reading beside the graph. */
export const TRANSCRIPT_MIN = 42;
/** Share of the terminal the graph takes when there is room for it. */
const GRAPH_SHARE = 0.4;

function statusColor(theme: Theme, status: AgentPhase['status']): string {
  if (status === 'active') return theme.success;
  if (status === 'completed') return theme.info;
  if (status === 'failed') return theme.error;
  if (status === 'cancelled' || status === 'interrupted') return theme.warning;
  return theme.textSubtle;
}

/**
 * The status marker and agent kind, with a selection caret prefixed when this
 * is the selected node. The caret is independent of `STATUS_MARKER`, which
 * encodes run status, not selection; before this, selecting a node changed
 * only border, background, and text color, invisible on a low-contrast
 * terminal. Follows the '›' precedent in theme-picker.ts and the hypothesis
 * drill-down (`experiment-log.ts#renderDetail`).
 */
export function nodeLabel(phase: AgentPhase, selected: boolean): string {
  return `${selected ? '› ' : ''}${STATUS_MARKER[phase.status]} ${phase.kind}`;
}

function edgeColor(theme: Theme, tone: EdgeTone): string {
  if (tone === 'failed') return theme.error;
  if (tone === 'live') return theme.accent;
  if (tone === 'done') return theme.info;
  return theme.borderStrong;
}

/**
 * A node's label at its widest, selected: caret, status marker, and kind. The
 * graph is sized for it, so no name is ever cut and picking a node never moves
 * the graph.
 */
function selectedLabelWidth(phase: AgentPhase): number {
  return nodeLabel(phase, true).length;
}

/**
 * Width for the Agents pane, or null when the terminal cannot carry a graph
 * that names every agent in full beside a readable transcript; the stacked list
 * takes over then. Derived from the terminal rather than fixed, so a wide
 * terminal gives the graph room while the transcript keeps its floor.
 */
export function agentPaneWidth(terminalWidth: number, phases: AgentPhase[]): number | null {
  const bounds = graphPaneBounds(phases, selectedLabelWidth);
  // Never narrower than the pane used to be: a one-stage round needs less room
  // than the heading above it, and a wrapped heading reads worse than slack.
  const floor = Math.max(bounds.min, STACKED_WIDTH);
  const ceiling = Math.max(bounds.max, STACKED_WIDTH);
  const room = terminalWidth - TRANSCRIPT_MIN;
  if (room < floor) return null;
  const share = Math.round(terminalWidth * GRAPH_SHARE);
  return Math.min(ceiling, room, Math.max(floor, share));
}

const AGENTS_TITLE = 'Agents';

export class AgentMapView {
  readonly output: BoxRenderable;
  /**
   * Everything this view draws, in a box that outlives what it draws. `#clear`
   * destroys the graph on every repaint, and while this pane is zoomed the
   * command column is a child of `output` too: without the separation the first
   * repaint after a zoom destroyed the command input along with the graph.
   */
  readonly #content: BoxRenderable;
  #theme: Theme;
  #renderedState: SessionState | null = null;
  #renderedWidth = 0;
  #renderedRows = 0;
  #elapsedTimer: ReturnType<typeof setInterval> | null = null;
  #runningRound: {round: RoundSummary; text: TextRenderable} | null = null;

  constructor(
    private readonly renderer: CliRenderer,
    private readonly controller: SessionController,
    theme: Theme,
  ) {
    this.#theme = theme;
    this.output = new BoxRenderable(renderer, {
      id: 'agent-map',
      width: STACKED_WIDTH,
      height: '100%',
      flexShrink: 0,
      flexDirection: 'column',
      paddingLeft: 1,
      paddingRight: 1,
      border: true,
      borderStyle: paneBorderStyle(false),
      borderColor: paneBorderColor(theme, false),
      title: paneTitle(AGENTS_TITLE, false),
      onMouseUp: () => this.controller.focusRound('agents'),
    });
    this.#content = new BoxRenderable(renderer, {
      id: 'agent-map-content',
      width: '100%',
      flexGrow: 1,
      flexShrink: 1,
      flexDirection: 'column',
      onMouseUp: () => this.controller.focusRound('agents'),
    });
    this.output.add(this.#content);
  }

  applyTheme(theme: Theme): void {
    this.#theme = theme;
    this.output.borderColor = theme.border;
    this.#renderedState = null;
  }

  /**
   * `rows` is the pane's height including its border. A round whose stages
   * stack taller than that is windowed rather than drawn off the bottom of the
   * pane; callers that manage their own height (tests driving the view
   * directly) can omit it and get the unclamped graph.
   */
  render(state: SessionState, widthOverride?: number, rows = Number.POSITIVE_INFINITY): void {
    const phases = visiblePhases(state);
    // The pane's width follows the terminal, so a resize has to redraw even
    // when the state is unchanged. A zoom hands the pane the whole terminal,
    // and one narrower than the graph needs stacks the agents rather than cut a
    // name.
    const width =
      widthOverride === undefined
        ? agentPaneWidth(this.renderer.terminalWidth, phases)
        : widthOverride >= graphPaneBounds(phases, selectedLabelWidth).min
          ? widthOverride
          : null;
    const paneWidth = widthOverride ?? width ?? STACKED_WIDTH;
    if (
      state === this.#renderedState &&
      paneWidth === this.#renderedWidth &&
      rows === this.#renderedRows
    ) {
      return;
    }
    // Selection and focus are drawn into the nodes, so a change to either is a
    // reason to redraw even when the phases are identical.
    this.#renderedState = state;
    this.#renderedWidth = paneWidth;
    this.#renderedRows = rows;
    this.output.width = paneWidth;
    // The pane that owns the arrow keys says so, the way every other focusable
    // surface in the client does. `focusedPane` is that single authority:
    // reading `roundFocus` directly lit this pane while a visualization too
    // narrow to split held the keys.
    applyPaneFocus(this.output, this.#theme, AGENTS_TITLE, focusedPane(state) === 'agents');
    this.#clear();
    if (phases.length === 0) {
      // A round the run has not reached has no agents, and never will until it
      // runs. "Waiting" would suggest something is on its way.
      const roundNumber = visibleRoundNumber(state);
      const round =
        roundNumber === null
          ? null
          : (stripRounds(state).find(item => item.number === roundNumber) ?? null);
      this.#content.add(
        new TextRenderable(this.renderer, {
          content:
            round?.status === 'planned'
              ? `Round ${roundNumber} has not run yet.`
              : 'Waiting for phases…',
          fg: this.#theme.textSubtle,
          width: '100%',
        }),
      );
      return;
    }

    const roundNumber = visibleRoundNumber(state);
    const round =
      roundNumber === null
        ? null
        : (scopedRounds(state).find(item => item.number === roundNumber) ?? null);
    const headingRow = new BoxRenderable(this.renderer, {
      id: 'agent-map-heading',
      width: '100%',
      flexDirection: 'row',
      justifyContent: 'space-between',
    });
    const headingText = headingLabel(roundNumber, round);
    const heading = new TextRenderable(this.renderer, {
      content: headingText,
      fg: this.#theme.textPrimary,
    });
    headingRow.add(heading);
    // What the round is made of, in one line. With one agent per stage it reads
    // as a summary; with a dozen it is the only way to see the round's shape
    // without counting nodes. It is the first thing to give up room, because a
    // wrapped heading costs a row of graph and says less.
    const summary = phaseSummary(phases);
    if (headingText.length + summary.length + 2 <= paneWidth - 4) {
      headingRow.add(
        new TextRenderable(this.renderer, {
          content: summary,
          fg: this.#theme.textMuted,
        }),
      );
    }
    this.#content.add(headingRow);
    // Elapsed time only advances while an agent is running, so the heading
    // ticks for exactly as long as one is.
    if (round !== null && hasActiveAgentTiming(round)) this.#runningRound = {round, text: heading};
    if (width === null) this.#renderStacked(phases, state.selectedAgentKind);
    else {
      this.#renderGraph(
        phases,
        state.selectedAgentKind,
        width,
        Math.max(0, rows - PANE_VCHROME - HEADING_ROWS),
      );
    }
    this.#syncElapsedTimer();
  }

  destroy(): void {
    this.#stopElapsedTimer();
  }

  /**
   * Stages left to right with the agents of a stage stacked inside their
   * column, laid out by `agent-graph.ts` and positioned absolutely: a graph has
   * no row-and-column structure for flex to follow.
   */
  #renderGraph(
    phases: AgentPhase[],
    selectedKind: string | null,
    paneWidth: number,
    graphRows: number,
  ): void {
    // A round whose stages stack (an interrupted attempt beside the one that
    // replaced it) is taller than the pane, and the layout is unbounded, so the
    // rows on hand decide what is drawn. Without this the extra nodes were laid
    // out past the bottom border and simply never seen.
    const fitted = graphWindow(phases, graphRows);
    if (fitted.hidden > 0) {
      this.#content.add(
        new TextRenderable(this.renderer, {
          // The oldest attempts are the ones dropped, so the count points up at
          // them.
          content: `↑ ${fitted.hidden}`,
          fg: this.#theme.textSubtle,
          width: '100%',
        }),
      );
    }
    const graph = layoutAgentGraph(fitted.phases, paneWidth - 4, selectedLabelWidth);
    // The graph sits in the middle of the pane rather than hugging the heading:
    // a chain is a few rows tall and a pane is not. `area` centres, `canvas`
    // gives the absolutely positioned cells their origin.
    const area = new BoxRenderable(this.renderer, {
      id: 'agent-graph',
      width: '100%',
      flexGrow: 1,
      flexShrink: 1,
      flexDirection: 'column',
      justifyContent: 'center',
      onMouseUp: () => this.controller.focusRound('agents'),
    });
    const canvas = new BoxRenderable(this.renderer, {
      id: 'agent-graph-canvas',
      width: '100%',
      height: graph.height,
      flexShrink: 0,
      onMouseUp: () => this.controller.focusRound('agents'),
    });
    this.#content.add(area);
    area.add(canvas);
    for (const run of edgeRuns(graph)) {
      canvas.add(
        new TextRenderable(this.renderer, {
          content: run.glyphs,
          fg: edgeColor(this.#theme, run.tone),
          position: 'absolute',
          left: run.x,
          top: run.y,
        }),
      );
    }
    for (const node of graph.nodes) {
      canvas.add(this.#renderNode(node.phase, node.phase.kind === selectedKind, node));
    }
  }

  #renderNode(
    phase: AgentPhase,
    selected: boolean,
    node: {x: number; y: number; width: number},
  ): BoxRenderable {
    const color = statusColor(this.#theme, phase.status);
    const box = new BoxRenderable(this.renderer, {
      id: `agent-${phase.kind}-${node.y}`,
      position: 'absolute',
      left: node.x,
      top: node.y,
      width: node.width,
      height: NODE_HEIGHT,
      flexDirection: 'column',
      // No horizontal padding: two columns of it is the difference between
      // "implementer" and "implement…" at the widths a four-stage round leaves.
      border: true,
      // Square, and not because of the fill: that sits on an inner layer
      // (tui-conventions.md), so the shape is free either way and this is a
      // look decision. A stage reads as a slot in a pipeline rather than as a
      // card, and the map's edges arrive at its sides. Unconditional rather
      // than square-only-when-selected, because swapping the shape on
      // selection reads as the node becoming a different kind of object,
      // which is why `PANE_BORDER` rejected the same swap for focus.
      borderStyle: 'single',
      borderColor: selected
        ? this.#theme.borderFocus
        : phase.status === 'pending'
          ? this.#theme.borderStrong
          : color,
      // Clicking a node filters the transcript to it, and clicking the selected
      // one clears the filter: the same toggle Tab and Esc give the keyboard.
      onMouseUp: () => this.controller.selectAgent(phase.kind),
    });
    // Inside the frame, so the fill stops at the border line instead of
    // painting the ring the edges arrive at (tui-conventions.md).
    if (selected) fillLayer(box, `agent-${phase.kind}-${node.y}-fill`, this.#theme.selectedSurface);
    const inner = node.width - 2;
    box.add(
      new TextRenderable(this.renderer, {
        content: truncate(nodeLabel(phase, selected), inner),
        fg: selected ? this.#theme.textStrong : color,
        width: '100%',
      }),
    );
    box.add(
      new TextRenderable(this.renderer, {
        content: truncate(phase.status, inner),
        fg: color,
        width: '100%',
      }),
    );
    // Always drawn, even when empty, so every node keeps the same height
    // (`NODE_HEIGHT`) whether or not it carries a runtime label.
    box.add(
      new TextRenderable(this.renderer, {
        content: truncate(agentRuntimeLabel(phase.provider, phase.model) ?? '', inner),
        fg: this.#theme.textMuted,
        width: '100%',
      }),
    );
    return box;
  }

  /** The pane before the graph: used when the terminal is too narrow for it. */
  #renderStacked(phases: AgentPhase[], selectedKind: string | null): void {
    for (const [index, phase] of phases.entries()) {
      this.#content.add(this.#renderStackedPhase(phase, selectedKind === phase.kind));
      if (index < phases.length - 1) {
        this.#content.add(
          new TextRenderable(this.renderer, {
            content: '        ↓',
            fg: this.#theme.textSubtle,
            width: '100%',
          }),
        );
      }
    }
  }

  #renderStackedPhase(phase: AgentPhase, selected: boolean): BoxRenderable {
    const row = new BoxRenderable(this.renderer, {
      id: `agent-${phase.kind}`,
      width: '100%',
      flexDirection: 'column',
      marginTop: 1,
      paddingLeft: 1,
      paddingRight: 1,
      // Passing borderStyle without border draws a frame that the layout does
      // not reserve rows for, and the phase's lines then overlap it.
      //
      // Square for the same reason as `#renderNode`: this is that node in the
      // stacked layout, so the two layouts read as one object.
      ...(selected
        ? {
            border: true,
            borderStyle: 'single' as const,
            borderColor: this.#theme.borderFocus,
          }
        : {}),
      onMouseUp: () => this.controller.selectAgent(phase.kind),
    });
    if (selected) fillLayer(row, `agent-${phase.kind}-fill`, this.#theme.selectedSurface);
    const color = statusColor(this.#theme, phase.status);
    row.add(
      new TextRenderable(this.renderer, {
        content: nodeLabel(phase, selected),
        fg: selected ? this.#theme.textStrong : color,
        width: '100%',
      }),
    );
    row.add(
      new TextRenderable(this.renderer, {
        content: phase.status,
        fg: color,
        width: '100%',
      }),
    );
    if (phase.roundLabel) {
      row.add(
        new TextRenderable(this.renderer, {
          content: phase.roundLabel,
          fg: this.#theme.textMuted,
          width: '100%',
        }),
      );
    }
    const runtimeLabel = agentRuntimeLabel(phase.provider, phase.model);
    if (runtimeLabel !== null) {
      row.add(
        new TextRenderable(this.renderer, {
          content: runtimeLabel,
          fg: this.#theme.textMuted,
          width: '100%',
        }),
      );
    }
    return row;
  }

  #syncElapsedTimer(): void {
    if (this.#runningRound === null || this.#elapsedTimer !== null) return;
    this.#elapsedTimer = setInterval(() => {
      if (this.#runningRound === null) return;
      const {round, text} = this.#runningRound;
      text.content = headingLabel(round.number, round);
    }, 1000);
  }

  #stopElapsedTimer(): void {
    if (this.#elapsedTimer === null) return;
    clearInterval(this.#elapsedTimer);
    this.#elapsedTimer = null;
  }

  #clear(): void {
    this.#runningRound = null;
    this.#stopElapsedTimer();
    for (const child of [...this.#content.getChildren()]) {
      this.#content.remove(child);
      child.destroyRecursively();
    }
  }
}

/**
 * Edge cells grouped into horizontal runs of one tone, so a straight edge is
 * one renderable rather than one per cell.
 */
export function edgeRuns(
  graph: AgentGraph,
): Array<{x: number; y: number; glyphs: string; tone: EdgeTone}> {
  const sorted = [...graph.cells].sort((a, b) => a.y - b.y || a.x - b.x);
  const runs: Array<{x: number; y: number; glyphs: string; tone: EdgeTone}> = [];
  for (const cell of sorted) {
    const open = runs.at(-1);
    if (
      open !== undefined &&
      open.y === cell.y &&
      open.tone === cell.tone &&
      open.x + open.glyphs.length === cell.x
    ) {
      open.glyphs += cell.glyph;
      continue;
    }
    runs.push({x: cell.x, y: cell.y, glyphs: cell.glyph, tone: cell.tone});
  }
  return runs;
}

/** `4 agents · 1 active · 2 done`, with failures and skips only when they exist. */
export function phaseSummary(phases: AgentPhase[]): string {
  const count = (status: AgentPhase['status']): number =>
    phases.filter(phase => phase.status === status).length;
  const parts = [
    `${phases.length} ${phases.length === 1 ? 'agent' : 'agents'}`,
    `${count('active')} active`,
    `${count('completed')} done`,
  ];
  if (count('failed') > 0) parts.push(`${count('failed')} failed`);
  if (count('cancelled') > 0) parts.push(`${count('cancelled')} cancelled`);
  if (count('interrupted') > 0) parts.push(`${count('interrupted')} interrupted`);
  const pending = count('pending');
  if (pending > 0) parts.push(`${pending} waiting`);
  return parts.join(' · ');
}

function truncate(text: string, width: number): string {
  const room = Math.max(1, width);
  return text.length <= room ? text : `${text.slice(0, room - 1)}…`;
}

/**
 * The agent-active elapsed time of the round on screen: wall clock minus the
 * gaps where no agent was running, which is what the round tabs report for the
 * live round.
 */
function headingLabel(roundNumber: number | null, round: RoundSummary | null): string {
  if (roundNumber === null) return 'Run flow';
  const elapsedMs = round === null ? 0 : roundAgentElapsedMs(round, new Date());
  if (elapsedMs <= 0) return `Round ${roundNumber} flow`;
  return `Round ${roundNumber} flow · ${elapsedLabel(elapsedMs)}`;
}
