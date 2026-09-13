import {afterEach, describe, expect, it} from 'bun:test';
import {BoxRenderable, rgbToHex} from '@opentui/core';
import {createTestRenderer, type TestRendererSetup} from '@opentui/core/testing';
import type {AgentPhase} from '@vibesys/core-state';
import type {SessionController} from '../session-controller.js';
import {initialSessionState, type SessionState} from '../session-model.js';
import {AgentMapView, nodeLabel} from './agent-map.js';
import {resolveTheme, shadowColor} from './theme.js';

// Kept apart from agent-map.test.ts (layout/width tests) so unrelated changes
// to that suite do not textually conflict with these selection-glyph tests.

/**
 * Before this, a selected node's only signal was border, background, and text
 * color: `STATUS_MARKER` encodes run status, never selection. `nodeLabel`
 * prefixes the selection caret independent of that marker, following the '›'
 * precedent in theme-picker.ts and the hypothesis drill-down.
 */
describe('nodeLabel', () => {
  function phase(status: AgentPhase['status']): AgentPhase {
    return {kind: 'implementer', status, roundNumber: null, roundLabel: null};
  }

  it('prefixes a caret only when selected, independent of the status marker', () => {
    expect(nodeLabel(phase('active'), false)).toBe('● implementer');
    expect(nodeLabel(phase('active'), true)).toBe('› ● implementer');
    expect(nodeLabel(phase('pending'), false)).toBe('○ implementer');
    expect(nodeLabel(phase('pending'), true)).toBe('› ○ implementer');
    expect(nodeLabel(phase('completed'), true)).toBe('› ✓ implementer');
    expect(nodeLabel(phase('failed'), true)).toBe('› × implementer');
  });
});

describe('agent node rendered selection glyph', () => {
  const cleanup: Array<() => void> = [];

  afterEach(() => {
    for (const destroy of cleanup.splice(0).reverse()) destroy();
  });

  /** Never fires in a render-only test; onMouseUp is never simulated. */
  const controller = {
    focusRound: () => {},
    selectAgent: () => {},
  } as unknown as SessionController;

  function stateWith(phases: AgentPhase[], selectedAgentKind: string | null): SessionState {
    const base = initialSessionState();
    return {...base, core: {...base.core, phases}, selectedAgentKind};
  }

  it('prefixes only the selected node with the caret, leaving the status marker alone', async () => {
    const testRenderer = await createTestRenderer({width: 100, height: 24});
    const view = new AgentMapView(testRenderer.renderer, controller, resolveTheme(null));
    testRenderer.renderer.root.add(view.output);
    cleanup.push(() => {
      view.destroy();
      view.output.destroyRecursively();
      testRenderer.renderer.destroy();
    });
    const phases: AgentPhase[] = [
      {kind: 'implementer', status: 'active', roundNumber: null, roundLabel: null},
      {kind: 'judge', status: 'pending', roundNumber: null, roundLabel: null},
    ];
    // A wide, explicit pane width keeps the graph layout (not the stacked
    // fallback) and gives each node room for its full label.
    view.render(stateWith(phases, 'implementer'), 60);
    await testRenderer.renderOnce();
    const frame = testRenderer.captureCharFrame();

    expect(frame).toContain('› ● implementer');
    expect(frame).toContain('○ judge');
    expect(frame).not.toContain('› ○ judge');
  });
});

/**
 * The selected node's fill-under-border and drop shadow: `agent-map.ts`'s
 * `#renderGraph`, `borderCoveringFill` (box-fill.ts) and `shadowCells`
 * (agent-graph.ts). `agent-map.test.ts` covers layout and width; the
 * selection-glyph tests above cover the caret. This covers the pixels: the
 * fill reaches the border ring, the border glyph keeps its own colour on top
 * of it, and the shadow lands only where `shadowCells` says it can.
 */
describe('selected agent node fill and drop shadow', () => {
  const cleanup: Array<() => void> = [];

  afterEach(() => {
    for (const destroy of cleanup.splice(0).reverse()) destroy();
  });

  const controller = {
    focusRound: () => {},
    selectAgent: () => {},
  } as unknown as SessionController;

  function stateWith(phases: AgentPhase[], selectedAgentKind: string | null): SessionState {
    const base = initialSessionState();
    return {...base, core: {...base.core, phases}, selectedAgentKind};
  }

  /** A three-stage chain: the middle stage has both an incoming and an outgoing edge. */
  function chain(): AgentPhase[] {
    return [
      {kind: 'orchestrator', status: 'completed', roundNumber: null, roundLabel: null},
      {kind: 'implementer', status: 'active', roundNumber: null, roundLabel: null},
      {kind: 'judge', status: 'pending', roundNumber: null, roundLabel: null},
    ];
  }

  /** A span's colours as lowercase hex, so they compare against theme values directly. */
  function spanAt(
    testRenderer: TestRendererSetup,
    row: number,
    col: number,
  ): {fg: string; bg: string} | undefined {
    const line = testRenderer.captureSpans().lines[row];
    if (line === undefined) return undefined;
    let cursor = 0;
    for (const span of line.spans) {
      if (col < cursor + span.width) {
        return {fg: rgbToHex(span.fg).toLowerCase(), bg: rgbToHex(span.bg).toLowerCase()};
      }
      cursor += span.width;
    }
    return undefined;
  }

  async function renderChain(selected: string | null): Promise<TestRendererSetup> {
    const testRenderer = await createTestRenderer({width: 100, height: 24});
    const view = new AgentMapView(testRenderer.renderer, controller, resolveTheme(null));
    testRenderer.renderer.root.add(view.output);
    cleanup.push(() => {
      view.destroy();
      view.output.destroyRecursively();
      testRenderer.renderer.destroy();
    });
    // A wide, explicit pane width keeps the graph layout, with plenty of
    // slack past the graph on every side (no `rows` argument, so the row
    // budget is unclamped): nothing here is testing the tight-bounds case.
    view.render(stateWith(chain(), selected), 100);
    await testRenderer.renderOnce();
    return testRenderer;
  }

  it("covers the selected node's own border ring with the fill, border glyph colour kept", async () => {
    const theme = resolveTheme(null);
    const testRenderer = await renderChain('implementer');
    const root = testRenderer.renderer.root;
    const node = root.findDescendantById('agent-implementer-0');
    const fill = root.findDescendantById('agent-implementer-0-fill');
    expect(node).toBeInstanceOf(BoxRenderable);
    expect(fill).toBeInstanceOf(BoxRenderable);
    if (!(node instanceof BoxRenderable) || !(fill instanceof BoxRenderable)) return;

    // The fill is a sibling sized to the node's own outer rectangle, not an
    // inner box (`borderCoveringFill`).
    expect({x: fill.x, y: fill.y, width: fill.width, height: fill.height}).toEqual({
      x: node.x,
      y: node.y,
      width: node.width,
      height: node.height,
    });

    const rows = testRenderer.captureCharFrame().split('\n');
    // Every corner of the node's own border still shows its glyph.
    expect(rows[node.y]?.[node.x]).toBe('┌');
    expect(rows[node.y]?.[node.x + node.width - 1]).toBe('┐');
    expect(rows[node.y + node.height - 1]?.[node.x]).toBe('└');
    expect(rows[node.y + node.height - 1]?.[node.x + node.width - 1]).toBe('┘');

    // The border glyph keeps the selected border colour, painted over the
    // fill's background rather than the fill erasing it.
    const corner = spanAt(testRenderer, node.y, node.x);
    expect(corner?.fg).toBe(theme.borderFocus.toLowerCase());
    expect(corner?.bg).toBe(theme.selectedSurface.toLowerCase());

    // An interior cell shows the same fill background, as it always has.
    const interior = spanAt(testRenderer, node.y + 1, node.x + 1);
    expect(interior?.bg).toBe(theme.selectedSurface.toLowerCase());
  });

  it('leaves an unselected node without a fill or a shadow', async () => {
    const theme = resolveTheme(null);
    const testRenderer = await renderChain('implementer');
    const root = testRenderer.renderer.root;
    const node = root.findDescendantById('agent-judge-0');
    expect(node).toBeInstanceOf(BoxRenderable);
    if (!(node instanceof BoxRenderable)) return;

    expect(root.findDescendantById('agent-judge-0-fill')).toBeUndefined();
    const corner = spanAt(testRenderer, node.y, node.x);
    expect(corner?.bg).not.toBe(theme.selectedSurface.toLowerCase());

    // Nothing in the column immediately right of it, or the row immediately
    // below it, carries a shadow glyph.
    const rows = testRenderer.captureCharFrame().split('\n');
    const rightColumn = rows
      .slice(node.y, node.y + node.height + 1)
      .map(row => row[node.x + node.width] ?? '')
      .join('');
    expect(rightColumn).not.toContain('▌');
    const belowRow = (rows[node.y + node.height] ?? '').slice(node.x, node.x + node.width + 1);
    expect(belowRow).not.toContain('▀');
  });

  it("shadows the selected node's right column and bottom row, skipping the edge's departure cell", async () => {
    const theme = resolveTheme(null);
    const shadow = shadowColor(theme).toLowerCase();
    const testRenderer = await renderChain('implementer');
    const root = testRenderer.renderer.root;
    const node = root.findDescendantById('agent-implementer-0');
    expect(node).toBeInstanceOf(BoxRenderable);
    if (!(node instanceof BoxRenderable)) return;

    const rows = testRenderer.captureCharFrame().split('\n');
    const rightX = node.x + node.width;
    const departureRow = node.y + 1;

    // The departure cell an outgoing edge already occupies keeps its own
    // glyph and colour: no shadow painted over it.
    expect(rows[departureRow]?.[rightX]).not.toBe('▌');
    expect(spanAt(testRenderer, departureRow, rightX)?.fg).not.toBe(shadow);

    // Every other row of the right column shows the shadow glyph and colour.
    for (let y = node.y + 2; y < node.y + node.height; y += 1) {
      expect(rows[y]?.[rightX]).toBe('▌');
      expect(spanAt(testRenderer, y, rightX)?.fg).toBe(shadow);
    }

    // The bottom row, one past the border, shows the shadow glyph across the
    // node's width, corner included.
    const bottomY = node.y + node.height;
    for (let x = node.x + 1; x <= node.x + node.width; x += 1) {
      expect(rows[bottomY]?.[x]).toBe('▀');
      expect(spanAt(testRenderer, bottomY, x)?.fg).toBe(shadow);
    }
  });
});
