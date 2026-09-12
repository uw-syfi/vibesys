import {afterEach, describe, expect, test} from 'bun:test';
import {createTestRenderer, type TestRendererSetup} from '@opentui/core/testing';
import type {AgentPhase} from '@vibesys/core-state';
import type {SessionController} from '../session-controller.js';
import {initialSessionState, type SessionState} from '../session-model.js';
import {SPINNER_FRAMES} from './activity-bar.js';
import {AgentMapView, agentPaneWidth, STACKED_WIDTH, TRANSCRIPT_MIN} from './agent-map.js';
import {resolveTheme} from './theme.js';

/** The active-node marker: `nodeLabel` draws the spinner's frame 0 here first. */
const activeMarker = SPINNER_FRAMES[0];

/** A round with one agent per kind, in order. */
function round(...kinds: string[]): AgentPhase[] {
  return kinds.map(kind => ({kind, status: 'completed', roundNumber: 1, roundLabel: null}));
}

const THREE = round('orchestrator', 'implementer', 'judge');
const FOUR = round('orchestrator', 'implementer', 'judge', 'profiler');

/**
 * The round view is agents -> transcript across the whole terminal, with the
 * rounds as a tab row above both. app.ts gives the agent pane
 * `agentPaneWidth(terminalWidth, phases)` and the transcript the remainder.
 */
describe('agentPaneWidth', () => {
  test('takes 40% of the terminal between its floor and its ceiling', () => {
    expect(agentPaneWidth(160, THREE)).toBe(64);
    expect(agentPaneWidth(210, FOUR)).toBe(84);
  });

  test('is never narrower than every agent named in full, nor wider than the stages use', () => {
    // 40% of 150 is 60, under the 63 columns that hold `› ✓ orchestrator`,
    // `› ✓ implementer` and `› ✓ judge`; 40% of 250 is 100, past the 68 that
    // three stages can use.
    expect(agentPaneWidth(150, THREE)).toBe(63);
    expect(agentPaneWidth(250, THREE)).toBe(68);
  });

  test('gives way to the stacked list where that floor and the transcript do not both fit', () => {
    expect(agentPaneWidth(104, THREE)).toBeNull();
    expect(agentPaneWidth(105, THREE)).toBe(63);
  });

  test('holds the transcript floor at every width, for every round shape', () => {
    const kinds = ['orchestrator', 'implementer', 'judge', 'profiler', 'mutator', 'perf_eval'];
    // 72 = STACKED_WIDTH (30) + TRANSCRIPT_MIN (42): the narrowest width where
    // both floors can coexist at all.
    for (let terminalWidth = 72; terminalWidth <= 200; terminalWidth += 1) {
      for (const stageCount of [0, 1, 2, 3, 4, 6]) {
        // app.ts turns a null pane width into the stacked fallback, so the
        // floor has to hold through that fallback too.
        const paneWidth =
          agentPaneWidth(terminalWidth, round(...kinds.slice(0, stageCount))) ?? STACKED_WIDTH;
        expect(terminalWidth - paneWidth).toBeGreaterThanOrEqual(TRANSCRIPT_MIN);
      }
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
    view.render({...stackedState(attempts), selectedAgentKind: selected}, width, ROWS);
    await testRenderer.renderOnce();
    const frame = testRenderer.captureCharFrame();
    // Every node draws its kind on its first row, so the markers count nodes.
    // The active attempt draws the spinner's frame 0, not the static `●`.
    const nodes = (frame.match(new RegExp(`[${activeMarker}!] implementer`, 'g')) ?? []).length;
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
      view.render(stackedState(6), 120, ROWS);
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
    expect(frame).toContain(`${activeMarker} implementer`);
    expect(frame).toContain('○ judge');
    expect(frame).not.toContain('…');
  });
});
