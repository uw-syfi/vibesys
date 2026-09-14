import {afterEach, describe, expect, test} from 'bun:test';
import {createTestRenderer, type TestRendererSetup} from '@opentui/core/testing';
import type {AgentPhase} from '@vibesys/core-state';
import type {SessionController} from '../session-controller.js';
import {initialSessionState, type SessionState} from '../session-model.js';
import {
  AgentMapView,
  agentGraphMinWidth,
  agentPaneCeiling,
  agentPaneFloor,
  agentPaneWidth,
  agentPaneWidthWithOverride,
  agentsPaneVisible,
  clampGraphWidthOverride,
  graphFits,
  STACKED_WIDTH,
  TRANSCRIPT_MIN,
} from './agent-map.js';
import {MIN_SPLIT_WIDTH} from './right-pane.js';
import {resolveTheme} from './theme.js';

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
 * The geometric bounds an explicit `<`/`>` override is clamped between.
 * Unlike `agentPaneWidth`'s own floor, these ignore whether a name fits: the
 * low bound is a node box plus its edge column with no label at all, and the
 * high bound is automatic sizing's own ceiling.
 */
describe('agentGraphMinWidth and agentPaneCeiling', () => {
  test('the geometric minimum is every column at its 14-column floor plus one gutter per gap', () => {
    // Three stages: 3*14 + 2 gutters(5) + PANE_CHROME(4) = 42 + 10 + 4 = 56.
    expect(agentGraphMinWidth(THREE)).toBe(56);
    // Four stages: 4*14 + 3*5 + 4 = 56 + 15 + 4 = 75.
    expect(agentGraphMinWidth(FOUR)).toBe(75);
    // Strictly below every name's own floor: there is room left to truncate.
    expect(agentGraphMinWidth(THREE)).toBeLessThan(agentPaneFloor(THREE));
    expect(agentGraphMinWidth(FOUR)).toBeLessThan(agentPaneFloor(FOUR));
  });

  test('the ceiling matches what agentPaneWidth already never grows past', () => {
    // 250 columns is far past what three stages can use; agentPaneWidth's own
    // test above pins that at 68.
    expect(agentPaneCeiling(THREE)).toBe(68);
    expect(agentPaneWidth(250, THREE)).toBe(agentPaneCeiling(THREE));
  });

  test("the geometric minimum keeps agentPaneFloor's STACKED_WIDTH floor", () => {
    // One stage asks for 14 + PANE_CHROME(4) = 18 columns of geometry, and a
    // round the run has not reached asks for the same. An override is a licence
    // to cut agent names, not the round heading's elapsed tail or the
    // empty-round placeholder, both of which wrap under 30 and cost a row.
    expect(agentGraphMinWidth(round('orchestrator'))).toBe(STACKED_WIDTH);
    expect(agentGraphMinWidth([])).toBe(STACKED_WIDTH);
    // The floor is `agentPaneFloor`'s own, so the two agree wherever geometry
    // does not already clear it.
    for (const phases of [[], round('orchestrator'), THREE, FOUR]) {
      expect(agentGraphMinWidth(phases)).toBeGreaterThanOrEqual(STACKED_WIDTH);
      expect(agentGraphMinWidth(phases)).toBeLessThanOrEqual(agentPaneFloor(phases));
    }
  });
});

/**
 * The precondition on the `<`/`>` keys: below it the pane is the stacked list
 * whatever the override says, so a press has to store nothing rather than a
 * width this terminal cannot draw.
 */
describe('graphFits', () => {
  test('is the terminal holding the narrowest graph beside a readable transcript', () => {
    // 56 + 42 = 98 for three stages.
    expect(graphFits(97, THREE)).toBe(false);
    expect(graphFits(98, THREE)).toBe(true);
    expect(graphFits(90, THREE)).toBe(false);
  });

  test('agrees with agentPaneWidthWithOverride giving up on an override', () => {
    for (let width = 60; width <= 140; width += 1) {
      expect(agentPaneWidthWithOverride(width, THREE, 60) === null).toBe(!graphFits(width, THREE));
    }
  });
});

describe('clampGraphWidthOverride', () => {
  test('holds the override between the geometric minimum and the ceiling, on a wide terminal', () => {
    const width = 200; // room = 158, wider than THREE's 68-column ceiling.
    expect(clampGraphWidthOverride(-1000, width, THREE)).toBe(agentGraphMinWidth(THREE));
    expect(clampGraphWidthOverride(1000, width, THREE)).toBe(agentPaneCeiling(THREE));
    // Inside the range, the request passes through unchanged.
    expect(clampGraphWidthOverride(60, width, THREE)).toBe(60);
  });

  test('never asks the transcript for less than TRANSCRIPT_MIN, even when the ceiling would allow it', () => {
    const width = 100; // room = 58, short of THREE's 68-column ceiling.
    expect(clampGraphWidthOverride(1000, width, THREE)).toBe(width - TRANSCRIPT_MIN);
  });
});

describe('agentPaneWidthWithOverride', () => {
  test('with no override, matches automatic sizing exactly', () => {
    for (const width of [80, 100, 105, 150, 160, 250]) {
      expect(agentPaneWidthWithOverride(width, THREE, null)).toBe(agentPaneWidth(width, THREE));
    }
  });

  test('an explicit override can draw a truncating graph where automatic sizing already gives up', () => {
    const width = 100; // room = 58: under THREE's 63-column floor (automatic
    // gives up to the stacked list), but over its 56-column geometric minimum.
    expect(agentPaneWidth(width, THREE)).toBeNull();
    const overridden = agentPaneWidthWithOverride(width, THREE, 1000);
    expect(overridden).not.toBeNull();
    expect(overridden as number).toBeLessThan(agentPaneFloor(THREE));
  });

  test('still falls back to the stacked list once not even the geometric minimum fits', () => {
    const width = 90; // room = 48, under THREE's 56-column geometric minimum.
    expect(agentPaneWidth(width, THREE)).toBeNull();
    expect(agentPaneWidthWithOverride(width, THREE, 1000)).toBeNull();
    expect(agentPaneWidthWithOverride(width, THREE, agentGraphMinWidth(THREE))).toBeNull();
  });
});

/**
 * The `<`/`>` keys must no-op wherever this is false, so it has to agree with
 * `app.ts`'s own decision about whether the Agents pane holds the content row.
 */
describe('agentsPaneVisible', () => {
  function baseState(): SessionState {
    return {...initialSessionState(), experimentLog: null};
  }

  test('is visible for the plain round view', () => {
    expect(agentsPaneVisible(baseState(), 120)).toBe(true);
  });

  test('is false while the experiment log is the landing view', () => {
    // initialSessionState() starts on the experiment log, same as the real app.
    expect(agentsPaneVisible(initialSessionState(), 120)).toBe(false);
  });

  test('is false when zoomed to a different pane', () => {
    const state = baseState();
    const zoomed = {...state, layout: {...state.layout, zoomedPane: 'transcript' as const}};
    expect(agentsPaneVisible(zoomed, 120)).toBe(false);
  });

  test('is true when zoomed to the agents pane itself, even at a narrow width', () => {
    const state = baseState();
    const zoomed = {...state, layout: {...state.layout, zoomedPane: 'agents' as const}};
    expect(agentsPaneVisible(zoomed, 50)).toBe(true);
  });

  test('is false once a visualization split has room to open beside it', () => {
    const state = baseState();
    const withPane = {
      ...state,
      layout: {
        ...state.layout,
        right: {
          view: 'perf' as const,
          title: 'Performance',
          content: '',
          pending: false,
          error: null,
        },
      },
    };
    expect(agentsPaneVisible(withPane, MIN_SPLIT_WIDTH)).toBe(false);
    // Below the split's own floor the visualization falls back to the modal it
    // always was, so the agents pane keeps the row instead.
    expect(agentsPaneVisible(withPane, MIN_SPLIT_WIDTH - 1)).toBe(true);
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
    expect(frame).toContain('● implementer');
    expect(frame).toContain('○ judge');
    expect(frame).not.toContain('…');
  });
});

/**
 * The one behavior change this feature makes to what the pane can ever draw:
 * an explicit override may render a graph narrower than every name, and only
 * then. Automatic sizing (`graphWidthOverride: null`) never does, at any
 * width: `agentPaneWidth`'s own floor already guarantees that, unchanged.
 */
describe('truncation under an explicit override', () => {
  const cleanup: Array<() => void> = [];

  afterEach(() => {
    for (const destroy of cleanup.splice(0).reverse()) destroy();
  });

  function roundState(graphWidthOverride: number | null): SessionState {
    const base = initialSessionState();
    return {
      ...base,
      experimentLog: null,
      selectedRound: 1,
      graphWidthOverride,
      core: {...base.core, rounds: [{number: 1, status: 'active'}], phases: THREE},
    };
  }

  async function renderAt(
    width: number,
    graphWidthOverride: number | null,
  ): Promise<{frame: string; paneWidth: number}> {
    const testRenderer: TestRendererSetup = await createTestRenderer({width, height: 20});
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
    view.render(roundState(graphWidthOverride), undefined, 20);
    await testRenderer.renderOnce();
    return {frame: testRenderer.captureCharFrame(), paneWidth: view.output.width as number};
  }

  test('truncates under a narrow explicit override, and never under automatic sizing at the same width', async () => {
    const width = 200;

    const automatic = await renderAt(width, null);
    expect(automatic.paneWidth).toBe(agentPaneWidth(width, THREE) as number);
    expect(automatic.frame).not.toContain('…');

    // -1000 clamps to the geometric minimum: every column pinned at its
    // 14-column floor, the narrowest a graph can draw.
    const overridden = await renderAt(width, -1000);
    expect(overridden.paneWidth).toBe(agentGraphMinWidth(THREE));
    expect(overridden.frame).toContain('…');
    // Still a graph, not the stacked list: boxes and edges, just narrower.
    expect(overridden.paneWidth).not.toBe(STACKED_WIDTH);
    expect(overridden.frame).toContain('▶');
    // `judge` is short enough to survive even the geometric minimum whole.
    expect(overridden.frame).toContain('judge');
  });

  test('the same state re-clamps a stale override if the terminal has since narrowed', async () => {
    // An override wide enough at 200 columns is past the ceiling `>` would
    // ever have let it reach at 100: the render clamps again rather than
    // trusting a number that predates the resize.
    const {paneWidth} = await renderAt(100, 68);
    expect(paneWidth).toBe(clampGraphWidthOverride(68, 100, THREE));
    expect(paneWidth).toBeLessThan(68);
  });
});

/**
 * The case the design change was for: a stage with more than one agent
 * feeding the next stage's single node (in-degree 2 there). The stacked list
 * cannot represent that at all (`↓` is one line to one line); the graph can,
 * narrowed or not, because `layoutAgentGraph` was never coupled to
 * `agentPaneWidth`'s no-truncation floor in the first place.
 */
describe('fan-in at a narrowed, overridden width', () => {
  const cleanup: Array<() => void> = [];

  afterEach(() => {
    for (const destroy of cleanup.splice(0).reverse()) destroy();
  });

  test('two attempts feeding one judge still draw as two boxes and two edges, not a lossy stack', async () => {
    const phases: AgentPhase[] = [
      {kind: 'orchestrator', status: 'completed', roundNumber: 1, roundLabel: 'round-1'},
      {
        kind: 'implementer',
        status: 'interrupted',
        roundNumber: 1,
        roundLabel: 'round-1-retry-1-implementer',
        executionId: 'e0',
      },
      {
        kind: 'implementer',
        status: 'active',
        roundNumber: 1,
        roundLabel: 'round-1-retry-2-implementer',
        executionId: 'e1',
      },
      {kind: 'judge', status: 'pending', roundNumber: 1, roundLabel: null},
    ];
    const base = initialSessionState();
    const state: SessionState = {
      ...base,
      experimentLog: null,
      selectedRound: 1,
      // The narrowest a graph can draw at all: stacking a second agent inside
      // the implementer column changes its height, not its width, so this is
      // the same 56 columns as the three-stage chain above.
      graphWidthOverride: agentGraphMinWidth(phases),
      core: {...base.core, rounds: [{number: 1, status: 'active'}], phases},
    };

    const testRenderer: TestRendererSetup = await createTestRenderer({width: 200, height: 20});
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
    view.render(state, undefined, 20);
    await testRenderer.renderOnce();
    const frame = testRenderer.captureCharFrame();

    expect(view.output.width).toBe(agentGraphMinWidth(phases));
    // Two separate implementer boxes: NODE_HEIGHT (5) + the 2-row gap between
    // stacked agents puts the second one 7 rows under the first.
    expect(testRenderer.renderer.root.findDescendantById('agent-implementer-0')).toBeDefined();
    expect(testRenderer.renderer.root.findDescendantById('agent-implementer-7')).toBeDefined();
    // Both attempts still reach the judge: one arrow head per fed node,
    // regardless of how many sources feed it (`agent-graph.test.ts` pins the
    // routing itself; this just confirms the wiring reaches it at this width).
    expect(frame).toContain('▶');
    expect(frame).toContain('judge');
  });
});
