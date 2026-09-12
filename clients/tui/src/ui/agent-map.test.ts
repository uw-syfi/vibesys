import {afterEach, describe, expect, test} from 'bun:test';
import {createTestRenderer, type TestRendererSetup} from '@opentui/core/testing';
import type {AgentPhase} from '@vibesys/core-state';
import type {SessionController} from '../session-controller.js';
import {initialSessionState, type SessionState} from '../session-model.js';
import {AgentMapView, agentPaneWidth, STACKED_WIDTH, TRANSCRIPT_MIN} from './agent-map.js';
import {RAIL_COMPACT_WIDTH, roundRailWidth} from './round-rail.js';
import {resolveTheme} from './theme.js';

/** A round with one agent per kind, in order. */
function round(...kinds: string[]): AgentPhase[] {
  return kinds.map(kind => ({kind, status: 'completed', roundNumber: 1, roundLabel: null}));
}

const THREE = round('orchestrator', 'implementer', 'judge');
const FOUR = round('orchestrator', 'implementer', 'judge', 'profiler');

/**
 * The round view lays out rail -> agents -> transcript. app.ts sizes the agent
 * pane against the room left of the rail (`terminalWidth - railWidth`), then the
 * transcript fills the remainder. These tests pin the pane's own sizing against
 * the room it is given, then reproduce that pipeline and pin the transcript
 * floor across the rail breakpoints: at widths 72-84 the compact rail must not
 * appear and squeeze the transcript below its minimum.
 */
describe('agentPaneWidth', () => {
  test('takes 55% of the room left of the rail between its floor and its ceiling', () => {
    expect(agentPaneWidth(118, THREE)).toBe(65);
    expect(agentPaneWidth(155, FOUR)).toBe(85);
  });

  test('is never narrower than every agent named in full, nor wider than the stages use', () => {
    // 55% of 105 is 58, under the 63 columns that hold `› ✓ orchestrator`,
    // `› ✓ implementer` and `› ✓ judge`; 55% of 250 is 138, past the 68 that
    // three stages can use.
    expect(agentPaneWidth(105, THREE)).toBe(63);
    expect(agentPaneWidth(250, THREE)).toBe(68);
  });

  test('gives way to the stacked list where that floor and the transcript do not both fit', () => {
    expect(agentPaneWidth(104, THREE)).toBeNull();
    expect(agentPaneWidth(105, THREE)).toBe(63);
  });
});

describe('agentPaneWidth transcript floor beside the rail', () => {
  const KINDS = ['orchestrator', 'implementer', 'judge', 'profiler', 'mutator', 'perf_eval'];

  /** The transcript width app.ts would give this terminal for a round shape. */
  function transcriptWidth(terminalWidth: number, stageCount: number): number {
    const railWidth = roundRailWidth(terminalWidth);
    const available = terminalWidth - railWidth;
    // app.ts turns a null pane width into the stacked fallback, exactly as the
    // finding described; the floor has to hold through that fallback too.
    const paneWidth =
      agentPaneWidth(available, round(...KINDS.slice(0, stageCount))) ?? STACKED_WIDTH;
    return available - paneWidth;
  }

  test('holds the transcript floor from the collapse width up, for every round shape', () => {
    // 72 = STACKED_WIDTH (30) + TRANSCRIPT_MIN (42): the narrowest width where
    // both floors can coexist at all. Below it neither the rail nor the agents
    // pane can keep the transcript readable, so the run view is not expected to.
    for (let terminalWidth = 72; terminalWidth <= 200; terminalWidth += 1) {
      for (const stageCount of [0, 1, 2, 3, 4, 6]) {
        expect(transcriptWidth(terminalWidth, stageCount)).toBeGreaterThanOrEqual(TRANSCRIPT_MIN);
      }
    }
  });

  test('keeps the exact widths the finding measured above the floor', () => {
    // The reviewer saw transcript widths 29, 37, and 41 at these terminals while
    // a 13-column rail was visible; the rail now collapses there instead.
    for (const terminalWidth of [72, 80, 84]) {
      expect(roundRailWidth(terminalWidth)).toBe(0);
      for (const stageCount of [1, 2, 3, 4]) {
        expect(transcriptWidth(terminalWidth, stageCount)).toBeGreaterThanOrEqual(TRANSCRIPT_MIN);
      }
    }
  });

  test('the compact rail only appears where both floors still fit beside it', () => {
    for (let terminalWidth = 60; terminalWidth <= 140; terminalWidth += 1) {
      if (roundRailWidth(terminalWidth) !== RAIL_COMPACT_WIDTH) continue;
      // rail + agents floor + transcript floor never exceeds the terminal.
      expect(terminalWidth - RAIL_COMPACT_WIDTH - STACKED_WIDTH).toBeGreaterThanOrEqual(
        TRANSCRIPT_MIN,
      );
    }
  });
});

/**
 * A killed attempt and the attempt that resumed it are two agents in one stage,
 * so a round can be taller than the pane. The symptom is stated in rows, so it
 * is reproduced through the real renderable tree at a fixed terminal size: the
 * frame is what says whether a node was drawn past the bottom border.
 */
describe('agent graph row budget', () => {
  const cleanup: Array<() => void> = [];

  afterEach(() => {
    for (const destroy of cleanup.splice(0).reverse()) destroy();
  });

  const ROWS = 20;

  /** One round whose implementer stage stacks `attempts` agents. */
  function stackedState(attempts: number): SessionState {
    const base = initialSessionState();
    const phases: AgentPhase[] = [
      {kind: 'orchestrator', status: 'completed', roundNumber: 1, roundLabel: 'round-1'},
      ...Array.from({length: attempts}, (_, index) => ({
        kind: 'implementer',
        status: (index === attempts - 1 ? 'active' : 'interrupted') as AgentPhase['status'],
        roundNumber: 1,
        roundLabel: `round-1-retry-${index + 1}-implementer`,
        executionId: `e${index}`,
      })),
      {kind: 'judge', status: 'pending', roundNumber: 1, roundLabel: null},
    ];
    return {
      ...base,
      experimentLog: null,
      selectedRound: 1,
      core: {...base.core, rounds: [{number: 1, status: 'active'}], phases},
    };
  }

  async function renderGraph(
    attempts: number,
    width = 120,
    selected: string | null = null,
  ): Promise<{frame: string; nodes: number}> {
    const testRenderer: TestRendererSetup = await createTestRenderer({width, height: ROWS});
    const view = new AgentMapView(
      testRenderer.renderer,
      {} as unknown as SessionController,
      resolveTheme(null),
    );
    testRenderer.renderer.root.add(view.output);
    cleanup.push(() => {
      view.destroy();
      view.output.destroyRecursively();
      testRenderer.renderer.destroy();
    });
    view.render({...stackedState(attempts), selectedAgentKind: selected}, width, 0, ROWS);
    await testRenderer.renderOnce();
    const frame = testRenderer.captureCharFrame();
    // Every node draws its kind on its first row, so the markers count nodes.
    const nodes = (frame.match(/[●!] implementer/g) ?? []).length;
    return {frame, nodes};
  }

  test('keeps a stacked round inside the pane and says what it left out', async () => {
    const {frame, nodes} = await renderGraph(6);
    const rows = frame.trimEnd().split('\n');

    // The pane's bottom border is the boundary: a node drawn past the rows on
    // hand lands on it, or off screen entirely.
    expect(rows).toHaveLength(ROWS);
    expect(rows[ROWS - 1]).toMatch(/^╰[─╯]*$/);
    // 20 rows minus two border rows minus the heading minus the count leaves 16,
    // which holds two five-row nodes and the two-row gap between them.
    expect(nodes).toBe(2);
    expect(frame).toContain('↑ 4');
    // The stage is still on screen, so selecting it still has something to
    // select: a column never loses its last node.
    expect(frame).toContain('orchestrator');
    expect(frame).toContain('judge');
  });

  test('draws a round that fits whole, with no count', async () => {
    const {frame, nodes} = await renderGraph(1);

    expect(nodes).toBe(1);
    expect(frame).not.toContain('↑');
  });

  test('redrawing a stacked round does not stack extra hidden-count rows', async () => {
    const testRenderer: TestRendererSetup = await createTestRenderer({width: 120, height: ROWS});
    const view = new AgentMapView(
      testRenderer.renderer,
      {} as unknown as SessionController,
      resolveTheme(null),
    );
    testRenderer.renderer.root.add(view.output);
    cleanup.push(() => {
      view.destroy();
      view.output.destroyRecursively();
      testRenderer.renderer.destroy();
    });

    const childCounts: number[] = [];
    let frame = '';
    for (let round = 0; round < 3; round += 1) {
      // A fresh state object each time, so the render-skip guard (`state ===
      // this.#renderedState`) does not short-circuit the redraw: each pass is a
      // real repaint, the way a running round's own state updates would be.
      view.render(stackedState(6), 120, 0, ROWS);
      await testRenderer.renderOnce();
      frame = testRenderer.captureCharFrame();
      childCounts.push(view.output.getChildren().length);
    }

    expect(frame.match(/↑ 4/g) ?? []).toHaveLength(1);
    expect(new Set(childCounts).size).toBe(1);
  });

  test('stacks a zoomed round narrower than its graph, every name in full', async () => {
    // A zoom hands the pane the whole terminal whatever its width, and at 50
    // columns the graph cannot hold `› ✓ orchestrator` beside the others.
    const {frame} = await renderGraph(1, 50, 'orchestrator');

    expect(frame).not.toContain('▶');
    expect(frame).toContain('› ✓ orchestrator');
    expect(frame).toContain('● implementer');
    expect(frame).toContain('○ judge');
    expect(frame).not.toContain('…');
  });
});
