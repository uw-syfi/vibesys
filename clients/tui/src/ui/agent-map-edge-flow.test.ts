import {afterEach, describe, expect, it, spyOn} from 'bun:test';
import type {Renderable} from '@opentui/core';
import {rgbToHex} from '@opentui/core';
import {createTestRenderer, type TestRendererSetup} from '@opentui/core/testing';
import type {AgentPhase} from '@vibesys/core-state';
import type {SessionController} from '../session-controller.js';
import {initialSessionState, type SessionState} from '../session-model.js';
import {AgentMapView} from './agent-map.js';
import {contrastRatio, listThemes, mix, resolveTheme} from './theme.js';

// Kept apart from agent-map-spinner.test.ts for the same reason that suite is
// split from agent-map.test.ts: unrelated changes to either should not
// textually conflict with these edge-flow-specific tests.

/** Mirrors the identity check in agent-map-spinner.test.ts: a tick must mutate
 * the existing renderable tree in place, never rebuild it. */
function descendants(node: Renderable): Renderable[] {
  return [node, ...node.getChildren().flatMap(descendants)];
}

/** One row of the frame, as cells in column order with their foreground colour. */
interface Cell {
  ch: string;
  fg: string;
}

function spanRows(setup: TestRendererSetup): Cell[][] {
  return setup.captureSpans().lines.map(line => {
    const cells: Cell[] = [];
    for (const span of line.spans) {
      for (const ch of span.text) cells.push({ch, fg: rgbToHex(span.fg).toLowerCase()});
    }
    return cells;
  });
}

/** Never fires in a render-only test; onMouseUp is never simulated. */
const controller = {
  focusRound: () => {},
  selectAgent: () => {},
} as unknown as SessionController;

function stateWith(phases: AgentPhase[]): SessionState {
  const base = initialSessionState();
  return {...base, core: {...base.core, phases}, selectedAgentKind: null};
}

function phase(kind: string, status: AgentPhase['status']): AgentPhase {
  return {kind, status, roundNumber: null, roundLabel: null};
}

describe('agent graph edge flow band', () => {
  const cleanup: Array<() => void> = [];

  afterEach(() => {
    for (const destroy of cleanup.splice(0).reverse()) destroy();
  });

  /**
   * Spies on `setInterval` before rendering so the view's one spinner/flow
   * timer (agent-map.ts `#syncSpinnerTimer`, shared by both concerns per the
   * design) can be advanced deterministically by calling its captured
   * callback directly, rather than racing real wall-clock timers the way
   * agent-map-spinner.test.ts does for its looser "the frame differs"
   * assertion. Here we need an exact single tick, so a real `setTimeout`
   * wait is the wrong tool.
   */
  async function setUp(
    phases: AgentPhase[],
    width = 100,
    override = 70,
  ): Promise<{view: AgentMapView; testRenderer: TestRendererSetup; tick: () => void}> {
    const setIntervalSpy = spyOn(globalThis, 'setInterval');
    const testRenderer = await createTestRenderer({width, height: 24});
    const view = new AgentMapView(testRenderer.renderer, controller, resolveTheme(null));
    testRenderer.renderer.root.add(view.output);
    cleanup.push(() => {
      view.destroy();
      view.output.destroyRecursively();
      testRenderer.renderer.destroy();
      setIntervalSpy.mockRestore();
    });
    view.render(stateWith(phases), override);
    await testRenderer.renderOnce();
    const call = setIntervalSpy.mock.calls[0];
    const tick = call?.[0] as (() => void) | undefined;
    return {
      view,
      testRenderer,
      tick: () => {
        expect(tick).toBeDefined();
        tick?.();
      },
    };
  }

  /** The completed-to-active edge's row: the arrowhead column, and the line
   * cell columns feeding it, left to right. */
  function edgeLine(rows: Cell[][]): {
    rowIndex: number;
    row: Cell[];
    arrowCol: number;
    lineCols: number[];
  } {
    const rowIndex = rows.findIndex(row => row.some(cell => cell.ch === '▶'));
    expect(rowIndex).toBeGreaterThanOrEqual(0);
    const row = rows[rowIndex] ?? [];
    const arrowCol = row.findIndex(cell => cell.ch === '▶');
    const lineCols: number[] = [];
    for (let col = arrowCol - 1; col >= 0 && row[col]?.ch === '─'; col -= 1) lineCols.unshift(col);
    return {rowIndex, row, arrowCol, lineCols};
  }

  it('rides a brighter/brightest 2-cell band toward the arrowhead, one cell per tick, wrapping at the end', async () => {
    const theme = resolveTheme(null);
    const live = theme.accent.toLowerCase();
    const brighter = mix(theme.accent, theme.textStrong, 0.35).toLowerCase();
    const brightest = mix(theme.accent, theme.textStrong, 0.7).toLowerCase();
    const {testRenderer, tick} = await setUp([
      phase('orchestrator', 'completed'),
      phase('implementer', 'active'),
    ]);

    const {rowIndex, arrowCol, lineCols} = edgeLine(spanRows(testRenderer));
    expect(lineCols.length).toBeGreaterThan(1);

    // Walk one full cycle plus one: the extra step confirms the wrap, where
    // the head returns to the first line cell and the tail (which does not
    // wrap) disappears rather than jumping to the far end.
    for (let step = 0; step <= lineCols.length; step += 1) {
      const row = spanRows(testRenderer)[rowIndex] ?? [];
      const headIndex = step % lineCols.length;
      for (const [index, col] of lineCols.entries()) {
        const expected =
          index === headIndex ? brightest : index === headIndex - 1 ? brighter : live;
        expect(row[col]?.ch).toBe('─');
        expect(row[col]?.fg).toBe(expected);
      }
      // The arrowhead's own glyph and colour never join the band.
      expect(row[arrowCol]?.ch).toBe('▶');
      expect(row[arrowCol]?.fg).toBe(live);
      tick();
      await testRenderer.renderOnce();
    }
  });

  it('mutates the edge run renderables in place: same instances before and after a tick', async () => {
    const {view, testRenderer, tick} = await setUp([
      phase('orchestrator', 'completed'),
      phase('implementer', 'active'),
    ]);
    const before = descendants(view.output);

    tick();
    await testRenderer.renderOnce();
    const after = descendants(view.output);

    expect(after.length).toBe(before.length);
    expect(after.length).toBeGreaterThan(5);
    for (const [index, node] of before.entries()) {
      expect(after[index]).toBe(node);
    }
  });

  it("never bands the active node's own outbound edge, across several ticks", async () => {
    const theme = resolveTheme(null);
    const brightest = mix(theme.accent, theme.textStrong, 0.7).toLowerCase();
    const {testRenderer, tick} = await setUp(
      [
        phase('orchestrator', 'completed'),
        phase('implementer', 'active'),
        phase('judge', 'pending'),
      ],
      120,
      90,
    );
    // Only one edge in this round can qualify (orchestrator -> implementer,
    // completed -> active): if the outbound implementer -> judge edge ever
    // carried a band too, this count would climb past one.
    for (let step = 0; step < 4; step += 1) {
      const cells = spanRows(testRenderer).flat();
      expect(cells.filter(cell => cell.fg === brightest).length).toBe(1);
      tick();
      await testRenderer.renderOnce();
    }
  });

  it('bands nothing and runs no timer while no node is active', async () => {
    const theme = resolveTheme(null);
    const brighter = mix(theme.accent, theme.textStrong, 0.35).toLowerCase();
    const brightest = mix(theme.accent, theme.textStrong, 0.7).toLowerCase();
    const setIntervalSpy = spyOn(globalThis, 'setInterval');
    const testRenderer = await createTestRenderer({width: 100, height: 24});
    const view = new AgentMapView(testRenderer.renderer, controller, theme);
    testRenderer.renderer.root.add(view.output);
    cleanup.push(() => {
      view.destroy();
      view.output.destroyRecursively();
      testRenderer.renderer.destroy();
      setIntervalSpy.mockRestore();
    });

    view.render(
      stateWith([phase('orchestrator', 'completed'), phase('implementer', 'completed')]),
      70,
    );
    await testRenderer.renderOnce();

    const cells = spanRows(testRenderer).flat();
    expect(cells.some(cell => cell.fg === brighter || cell.fg === brightest)).toBe(false);
    expect(setIntervalSpy).not.toHaveBeenCalled();
  });

  it('gives the band strictly more contrast against the canvas than the live colour, in every theme', () => {
    // Mixing toward textStrong rather than toward white: on a light theme
    // white would lower contrast against the canvas, so this pins the
    // direction, not just the two lift amounts.
    for (const theme of listThemes()) {
      const live = contrastRatio(theme.accent, theme.canvas);
      const brighter = contrastRatio(mix(theme.accent, theme.textStrong, 0.35), theme.canvas);
      const brightest = contrastRatio(mix(theme.accent, theme.textStrong, 0.7), theme.canvas);
      expect(brighter).toBeGreaterThan(live);
      expect(brightest).toBeGreaterThan(brighter);
    }
  });
});
