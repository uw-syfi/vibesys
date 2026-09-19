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
  experimentLogVisible,
  focusedPane,
  scopedRounds,
  stripRounds,
  visiblePhases,
  visibleRoundNumber,
} from '../session-model.js';
import {SPINNER_FRAMES, SPINNER_INTERVAL_MS} from './activity-bar.js';
import {
  type AgentGraph,
  type EdgeTone,
  graphPaneBounds,
  graphWindow,
  layoutAgentGraph,
  NODE_HEIGHT,
  selectionBackdrop,
} from './agent-graph.js';
import {agentRuntimeLabel} from './agent-runtime-label.js';
import {fillLayer} from './box-fill.js';
import {applyPaneFocus, paneBorderColor, paneBorderStyle, paneTitle} from './focus.js';
import {elapsedLabel} from './previews.js';
import {splitFits} from './right-pane.js';
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
/** Share of the room left of the rail the graph takes when there is room for it. */
const GRAPH_SHARE = 0.55;

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
 *
 * An active phase draws the current spinner frame in the marker's cell
 * instead of the static `●`, so a phase that is actually running looks like
 * it: same cell, same width (every `SPINNER_FRAMES` glyph is one column, like
 * every other `STATUS_MARKER`), one glyph animating in place of another.
 * `spinnerFrame` defaults to the frame every view starts on.
 */
export function nodeLabel(phase: AgentPhase, selected: boolean, spinnerFrame = 0): string {
  const marker =
    phase.status === 'active'
      ? (SPINNER_FRAMES[spinnerFrame % SPINNER_FRAMES.length] ??
        SPINNER_FRAMES[0] ??
        STATUS_MARKER.active)
      : STATUS_MARKER[phase.status];
  return `${selected ? '› ' : ''}${marker} ${phase.kind}`;
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
 * The width at which every agent is already named in full: `graphPaneBounds`'s
 * floor, using the selected-label width every node is sized for. Never
 * narrower than the pane used to be: a one-stage round needs less room than
 * the heading above it, and a wrapped heading reads worse than slack. Below
 * this width a label starts losing characters to `…`.
 */
export function agentPaneFloor(phases: AgentPhase[]): number {
  return Math.max(graphPaneBounds(phases, selectedLabelWidth).min, STACKED_WIDTH);
}

/**
 * The width past which more columns stop being information: `graphPaneBounds`'s
 * own `max`, the point its docstring already names as "extra columns add
 * padding rather than information." Automatic sizing (`agentPaneWidth`) never
 * grows past this either, so it is the natural ceiling for an explicit
 * `<`/`>` override too (`clampGraphWidthOverride`): `>` stops here rather than
 * padding, and never asks for less than automatic sizing would already give on
 * the same terminal.
 */
export function agentPaneCeiling(phases: AgentPhase[]): number {
  return Math.max(graphPaneBounds(phases, selectedLabelWidth).max, STACKED_WIDTH);
}

/**
 * The narrowest pane an explicit `<`/`>` override may ask for: the wider of two
 * floors, so this is not in general the narrowest a graph can be laid out in.
 *
 * The geometric floor is that width: every stage column at its own 14-column
 * minimum (`NODE_WIDTH_MIN` in `agent-graph.ts`) plus one gutter of
 * edge-routing room between each pair of columns, ignoring names entirely
 * (`graphPaneBounds` with no `labelWidth` gives exactly this). Under it
 * `fitNodeWidths` still refuses to shrink a column past its own minimum, so the
 * columns plus gutters would add up to more than the pane was given and
 * overflow its border instead of fitting inside it.
 *
 * `STACKED_WIDTH` is the other floor, carried for the same reason
 * `agentPaneFloor` carries it: what an override buys is truncated agent names,
 * and nothing else in the pane. Geometry alone asks for 18 columns at one stage
 * and at a round that has not run, narrow enough to drop the heading's elapsed
 * tail and wrap the empty-round placeholder onto a second row, which costs a
 * graph row rather than revealing anything. From two stages up the geometric
 * floor is the wider of the two and this one never binds.
 */
export function agentGraphMinWidth(phases: AgentPhase[]): number {
  return Math.max(graphPaneBounds(phases).min, STACKED_WIDTH);
}

/**
 * Every `availableWidth` below is the terminal width minus the rounds rail's
 * column (`roundRailColumns` in `round-rail.ts`), so the graph and the width
 * keys size against what the rail leaves and the transcript keeps its floor.
 * The rail itself keeps its fixed width; `<`/`>`/`=` only move the panes.
 *
 * True while the room can carry a graph at all: even the narrowest one
 * needs `TRANSCRIPT_MIN` columns left over for the transcript beside it. When
 * this is false the stacked list is the pane, no override can change that, and
 * `<`/`>` therefore store nothing rather than leaving behind a width this
 * terminal was never allowed to draw.
 */
export function graphFits(availableWidth: number, phases: AgentPhase[]): boolean {
  return availableWidth - TRANSCRIPT_MIN >= agentGraphMinWidth(phases);
}

/**
 * Width for the Agents pane, or null when the room cannot carry a graph that
 * names every agent in full beside a readable transcript; the stacked list
 * takes over then. Derived from the room rather than fixed, so a wide terminal
 * gives the graph room while the transcript keeps its floor.
 */
export function agentPaneWidth(availableWidth: number, phases: AgentPhase[]): number | null {
  const floor = agentPaneFloor(phases);
  const ceiling = agentPaneCeiling(phases);
  const room = availableWidth - TRANSCRIPT_MIN;
  if (room < floor) return null;
  const share = Math.round(availableWidth * GRAPH_SHARE);
  return Math.min(ceiling, room, Math.max(floor, share));
}

/**
 * Clamps a requested `<`/`>` override to a range that can actually be drawn:
 * never narrower than `agentGraphMinWidth` (a node box plus its edge column),
 * never wider than `agentPaneCeiling` (automatic sizing's own ceiling, past
 * which more width is padding, not information), and never so wide the
 * transcript would drop under `TRANSCRIPT_MIN`. The low and high bounds can
 * invert on a terminal too narrow to hold even the low bound beside a
 * readable transcript; the high bound then collapses to the low one rather
 * than under it, and it is `agentPaneWidthWithOverride`'s job to notice the
 * terminal cannot draw a graph at all in that case and fall back to the
 * stacked list.
 */
export function clampGraphWidthOverride(
  requested: number,
  availableWidth: number,
  phases: AgentPhase[],
): number {
  const low = agentGraphMinWidth(phases);
  const high = Math.max(low, Math.min(agentPaneCeiling(phases), availableWidth - TRANSCRIPT_MIN));
  return Math.min(high, Math.max(low, requested));
}

/**
 * The Agents pane's width honoring an explicit `<`/`>` override, or `null`
 * for #686's automatic sizing (`agentPaneWidth`) when there is none. `null`
 * also covers the terminal being too narrow to draw a graph at all (not even
 * `agentGraphMinWidth` fits beside a readable transcript), in which case an
 * override cannot rescue it either, and the stacked list takes over exactly
 * as it does automatically.
 */
export function agentPaneWidthWithOverride(
  availableWidth: number,
  phases: AgentPhase[],
  override: number | null,
): number | null {
  if (override === null) return agentPaneWidth(availableWidth, phases);
  if (!graphFits(availableWidth, phases)) return null;
  return clampGraphWidthOverride(override, availableWidth, phases);
}

/**
 * True while the Agents pane holds a slot in the content row: not eclipsed by
 * the experiment log, not zoomed to a different pane, and not squeezed out by
 * a visualization split that has room to open beside it. `app.ts`'s own
 * `showAgents` and the `<`/`>` resize keys both read this, so a layout change
 * cannot leave the render and the keybinding guard disagreeing about whether
 * there is a pane on screen to resize.
 */
export function agentsPaneVisible(state: SessionState, terminalWidth: number): boolean {
  if (experimentLogVisible(state)) return false;
  const {zoomedPane, right} = state.layout;
  if (zoomedPane !== null) return zoomedPane === 'agents';
  return !(right !== null && splitFits(terminalWidth));
}

export interface AgentMapLayout {
  paneWidth: number;
  graphWidth: number | null;
  graphRows: number;
}

/**
 * Resolve the pane geometry once, before rendering chooses an empty, stacked,
 * or graph presentation. `graphWidthOverride` is the user's `<`/`>` width, honored
 * only outside zoom. An explicit width comes from zoom mode: it always
 * owns the pane width, but still falls back to the stacked presentation when
 * the graph cannot name every agent in full.
 */
export function agentMapLayout(
  availableWidth: number,
  phases: AgentPhase[],
  widthOverride: number | undefined,
  rows: number,
  graphWidthOverride: number | null = null,
): AgentMapLayout {
  const graphWidth =
    widthOverride === undefined
      ? agentPaneWidthWithOverride(availableWidth, phases, graphWidthOverride)
      : widthOverride >= graphPaneBounds(phases, selectedLabelWidth).min
        ? widthOverride
        : null;
  return {
    paneWidth: widthOverride ?? graphWidth ?? STACKED_WIDTH,
    graphWidth,
    graphRows: Math.max(0, rows - PANE_VCHROME - HEADING_ROWS),
  };
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
  #renderedFocus = false;
  #elapsedTimer: ReturnType<typeof setInterval> | null = null;
  #runningRound: {round: RoundSummary; text: TextRenderable} | null = null;
  #spinnerFrame = 0;
  #spinnerTimer: ReturnType<typeof setInterval> | null = null;
  /** Every active node's marker cell, refreshed in place on the spinner tick. */
  #spinnerNodes: Array<{
    phase: AgentPhase;
    selected: boolean;
    text: TextRenderable;
    inner: number | null;
  }> = [];

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
   * `railWidth` is the column the rounds rail has taken, 0 when it is off
   * screen. It sizes this pane against what is left, so the transcript keeps
   * its floor beside a rail rather than being squeezed by it, and it says
   * whether the rail is a surface the round keys can be on.
   *
   * `rows` is the pane's height including its border, the same budget the rail
   * draws from. A round whose stages stack taller than that is windowed rather
   * than drawn off the bottom of the pane; callers that manage their own height
   * (tests driving the view directly) can omit it and get the unclamped graph.
   */
  render(
    state: SessionState,
    widthOverride?: number,
    railWidth = 0,
    rows = Number.POSITIVE_INFINITY,
  ): void {
    const phases = visiblePhases(state);
    // The pane's width follows the terminal, so a resize has to redraw even
    // when the state is unchanged. A zoom hands the pane the whole terminal
    // (`widthOverride`, this method's own parameter for that, distinct from
    // `state.graphWidthOverride` below), and one narrower than the graph needs
    // stacks the agents rather than cut a name.
    // Null either way means the stacked list: the pane is drawn at `paneWidth`
    // whatever that decides.
    // The graph is sized against the room left of the rail, so the transcript
    // keeps its floor beside it; the rail's own width is fixed and no width
    // control moves it.
    const layout = agentMapLayout(
      this.renderer.terminalWidth - railWidth,
      phases,
      widthOverride,
      rows,
      state.graphWidthOverride,
    );
    // A stale `rounds` focus lands here once the rail goes off screen, so the
    // border follows the keys rather than the raw field: `keybindings` drives
    // this pane in exactly that case, and a round view with no focus border on
    // any pane is a view that does not say where its arrows go.
    const focused =
      focusedPane(state) === 'agents' && !(state.roundFocus === 'rounds' && railWidth > 0);
    if (
      state === this.#renderedState &&
      layout.paneWidth === this.#renderedWidth &&
      rows === this.#renderedRows &&
      focused === this.#renderedFocus
    ) {
      return;
    }
    // Selection and focus are drawn into the nodes, so a change to either is a
    // reason to redraw even when the phases are identical.
    this.#renderedState = state;
    this.#renderedWidth = layout.paneWidth;
    this.#renderedRows = rows;
    this.#renderedFocus = focused;
    this.output.width = layout.paneWidth;
    // The pane that owns the arrow keys says so, the way every other focusable
    // surface in the client does. `focusedPane` is that single authority:
    // reading `roundFocus` directly lit this pane while a visualization too
    // narrow to split held the keys.
    applyPaneFocus(this.output, this.#theme, AGENTS_TITLE, focused);
    this.#clear();
    if (phases.length === 0) {
      this.#renderEmptyState(state);
      return;
    }

    this.#renderHeading(state, phases, layout.paneWidth);
    this.#renderPhases(phases, state.selectedAgentKind, layout);
    this.#syncElapsedTimer();
    this.#syncSpinnerTimer();
  }

  #renderEmptyState(state: SessionState): void {
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
  }

  #renderHeading(state: SessionState, phases: AgentPhase[], paneWidth: number): void {
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
  }

  #renderPhases(phases: AgentPhase[], selectedKind: string | null, layout: AgentMapLayout): void {
    if (layout.graphWidth === null) this.#renderStacked(phases, selectedKind);
    else {
      this.#renderGraph(phases, selectedKind, layout.graphWidth, layout.graphRows);
    }
  }

  destroy(): void {
    this.#stopElapsedTimer();
    this.#stopSpinnerTimer();
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
          // them the way the rounds rail points at the rounds above its window.
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
    // The canvas box is only ever as tall as the graph itself (`height:
    // graph.height` above), with no row held back for a backdrop past the
    // tallest column, so a backdrop cell's real safety bound is the space
    // this method was actually given, not the graph's own footprint.
    const bounds = {width: paneWidth - 4, height: graphRows};
    const selectedNode = graph.nodes.find(node => node.phase.kind === selectedKind);
    if (selectedNode !== undefined) {
      // Painted first, so it sits behind the edges and the nodes drawn below:
      // an edge or arrowhead cell that lands on it keeps its own glyph and
      // foreground and simply picks up this background (`selectionBackdrop`).
      // The bottom row is an upper-half block in the surface colour instead
      // (`BackdropCell.half`).
      for (const cell of selectionBackdrop(graph, selectedNode, bounds)) {
        canvas.add(
          new TextRenderable(this.renderer, {
            content: cell.half ? '▀' : ' ',
            ...(cell.half ? {fg: this.#theme.selectedSurface} : {bg: this.#theme.selectedSurface}),
            position: 'absolute',
            left: cell.x,
            top: cell.y,
          }),
        );
      }
    }
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
      // (tui/conventions.md), so the shape is free either way and this is a
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
    // No interior fill: the selected node stays plain canvas inside its
    // border. Its backdrop is a separate rectangle drawn behind everything by
    // `#renderGraph` (`selectionBackdrop`).
    const inner = node.width - 2;
    const label = new TextRenderable(this.renderer, {
      content: truncate(nodeLabel(phase, selected, this.#spinnerFrame), inner),
      fg: selected ? this.#theme.textStrong : color,
      width: '100%',
    });
    box.add(label);
    if (phase.status === 'active') this.#spinnerNodes.push({phase, selected, text: label, inner});
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
    const label = new TextRenderable(this.renderer, {
      content: nodeLabel(phase, selected, this.#spinnerFrame),
      fg: selected ? this.#theme.textStrong : color,
      width: '100%',
    });
    row.add(label);
    if (phase.status === 'active')
      this.#spinnerNodes.push({phase, selected, text: label, inner: null});
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

  /**
   * Mutates every active node's marker cell in place, the same idiom as
   * `#syncElapsedTimer`, so a running phase animates without the 120ms tick
   * ever calling `render()` and rebuilding the graph eight times a second.
   * Started only while a phase is active, stopped the moment none is.
   */
  #syncSpinnerTimer(): void {
    if (this.#spinnerNodes.length === 0) {
      this.#stopSpinnerTimer();
      return;
    }
    if (this.#spinnerTimer !== null) return;
    this.#spinnerTimer = setInterval(() => {
      this.#spinnerFrame = (this.#spinnerFrame + 1) % SPINNER_FRAMES.length;
      for (const node of this.#spinnerNodes) {
        const label = nodeLabel(node.phase, node.selected, this.#spinnerFrame);
        node.text.content = node.inner === null ? label : truncate(label, node.inner);
      }
    }, SPINNER_INTERVAL_MS);
  }

  #stopSpinnerTimer(): void {
    if (this.#spinnerTimer === null) return;
    clearInterval(this.#spinnerTimer);
    this.#spinnerTimer = null;
  }

  #clear(): void {
    this.#runningRound = null;
    this.#stopElapsedTimer();
    this.#spinnerNodes = [];
    this.#stopSpinnerTimer();
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
 * gaps where no agent was running, which is what the rounds rail reports for
 * the running round.
 */
function headingLabel(roundNumber: number | null, round: RoundSummary | null): string {
  if (roundNumber === null) return 'Run flow';
  const elapsedMs = round === null ? 0 : roundAgentElapsedMs(round, new Date());
  if (elapsedMs <= 0) return `Round ${roundNumber} flow`;
  return `Round ${roundNumber} flow · ${elapsedLabel(elapsedMs)}`;
}
