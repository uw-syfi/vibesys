import {afterEach, describe, expect, test} from 'bun:test';
import {createTestRenderer, type TestRendererSetup} from '@opentui/core/testing';
import type {AgentPhase} from '@vibesys/core-state';
import type {SessionController} from '../session-controller.js';
import {initialSessionState, type SessionState} from '../session-model.js';
import {AgentMapView, agentPaneWidth, STACKED_WIDTH, TRANSCRIPT_MIN} from './agent-map.js';
import {RAIL_COMPACT_WIDTH, roundRailWidth} from './round-rail.js';
import {resolveTheme} from './theme.js';

/**
 * Runs `body` with the agent graph pane opted in or out. The pane is off by
 * default now (the rail lists a round's agents instead), so the graph's own
 * width pipeline has to ask for it.
 */
function withGraph<T>(value: string | undefined, body: () => T): T {
  const previous = process.env['VIBESYS_AGENT_GRAPH'];
  if (value === undefined) delete process.env['VIBESYS_AGENT_GRAPH'];
  else process.env['VIBESYS_AGENT_GRAPH'] = value;
  try {
    return body();
  } finally {
    if (previous === undefined) delete process.env['VIBESYS_AGENT_GRAPH'];
    else process.env['VIBESYS_AGENT_GRAPH'] = previous;
  }
}

/**
 * With the graph pane opted in, the round view lays out rail -> agents ->
 * transcript. app.ts sizes the agent pane against the room left of the rail
 * (`terminalWidth - railWidth`), then the transcript fills the remainder. These
 * tests reproduce that pipeline and pin the transcript floor across the rail
 * breakpoints: at widths 72-84 the compact rail must not appear and squeeze the
 * transcript below its minimum.
 */
describe('agentPaneWidth transcript floor beside the rail', () => {
  /** The transcript width app.ts would give this terminal for a round shape. */
  function transcriptWidth(terminalWidth: number, stageCount: number): number {
    const railWidth = roundRailWidth(terminalWidth);
    const available = terminalWidth - railWidth;
    // app.ts turns a null pane width into the stacked fallback, exactly as the
    // finding described; the floor has to hold through that fallback too.
    const paneWidth = agentPaneWidth(available, stageCount) ?? STACKED_WIDTH;
    return available - paneWidth;
  }

  test('holds the transcript floor from the collapse width up, for every round shape', () => {
    withGraph('1', () => {
      // 72 = STACKED_WIDTH (30) + TRANSCRIPT_MIN (42): the narrowest width where
      // both floors can coexist at all. Below it neither the rail nor the agents
      // pane can keep the transcript readable, so the run view is not expected to.
      for (let terminalWidth = 72; terminalWidth <= 140; terminalWidth += 1) {
        for (const stageCount of [1, 2, 3, 4, 6]) {
          expect(transcriptWidth(terminalWidth, stageCount)).toBeGreaterThanOrEqual(TRANSCRIPT_MIN);
        }
      }
    });
  });

  test('keeps the exact widths the finding measured above the floor', () => {
    withGraph('1', () => {
      // The reviewer saw transcript widths 29, 37, and 41 at these terminals while
      // a 13-column rail was visible; the rail now collapses there instead.
      for (const terminalWidth of [72, 80, 84]) {
        expect(roundRailWidth(terminalWidth)).toBe(0);
        for (const stageCount of [1, 2, 3, 4]) {
          expect(transcriptWidth(terminalWidth, stageCount)).toBeGreaterThanOrEqual(TRANSCRIPT_MIN);
        }
      }
    });
  });

  test('the compact rail only appears where both floors still fit beside it', () => {
    withGraph('1', () => {
      for (let terminalWidth = 60; terminalWidth <= 140; terminalWidth += 1) {
        if (roundRailWidth(terminalWidth) !== RAIL_COMPACT_WIDTH) continue;
        // rail + agents floor + transcript floor never exceeds the terminal.
        expect(terminalWidth - RAIL_COMPACT_WIDTH - STACKED_WIDTH).toBeGreaterThanOrEqual(
          TRANSCRIPT_MIN,
        );
      }
    });
  });
});

/**
 * What the change buys, held as a number rather than a claim.
 *
 * The baseline is the round view this change replaces: a 28 column rail, then
 * an agents pane sized by `agentPaneWidth` out of what is left, then the
 * transcript. `agentPaneWidth` itself is unchanged, so the comparison is real
 * and not a restatement of the new constants.
 */
describe('columns the transcript reclaims', () => {
  const BASELINE_RAIL_FULL = 28;
  const BASELINE_RAIL_COMPACT = 13;

  /** Transcript width before this change: rail, agents pane, then the rest. */
  function baseline(terminalWidth: number, stageCount: number): number {
    const budget = STACKED_WIDTH + TRANSCRIPT_MIN;
    const rail =
      terminalWidth >= BASELINE_RAIL_FULL + budget
        ? BASELINE_RAIL_FULL
        : terminalWidth >= BASELINE_RAIL_COMPACT + budget
          ? BASELINE_RAIL_COMPACT
          : 0;
    const available = terminalWidth - rail;
    return available - (agentPaneWidth(available, stageCount) ?? STACKED_WIDTH);
  }

  /** Transcript width now: the rail, and everything else is transcript. */
  function reclaimed(terminalWidth: number): number {
    return withGraph(undefined, () => terminalWidth - roundRailWidth(terminalWidth));
  }

  test('a four stage round at 150 columns gives the transcript 69 more', () => {
    // The measurement quoted in #653: the graph took 75 of 150 columns and left
    // the transcript 47, five above its floor.
    expect(baseline(150, 4)).toBe(47);
    expect(reclaimed(150)).toBe(116);
  });

  test('the widest rounds gain the most, and no round shape loses at 150 up', () => {
    for (let terminalWidth = 150; terminalWidth <= 240; terminalWidth += 1) {
      for (const stageCount of [1, 2, 3, 4, 6]) {
        expect(reclaimed(terminalWidth)).toBeGreaterThan(baseline(terminalWidth, stageCount));
      }
    }
  });

  test('the transcript keeps its floor at every width the rail shows at', () => {
    // Honest about the narrow end: between 89 and 105 columns the rail is now
    // on screen where it used to be hidden, so it costs the transcript the
    // columns it takes. The floor is what must not move, and it does not.
    for (let terminalWidth = 20; terminalWidth <= 240; terminalWidth += 1) {
      const rail = withGraph(undefined, () => roundRailWidth(terminalWidth));
      if (rail === 0) continue;
      expect(reclaimed(terminalWidth)).toBeGreaterThanOrEqual(TRANSCRIPT_MIN);
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

  async function renderGraph(attempts: number): Promise<{frame: string; nodes: number}> {
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
    view.render(stackedState(attempts), 120, 0, ROWS);
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
});
