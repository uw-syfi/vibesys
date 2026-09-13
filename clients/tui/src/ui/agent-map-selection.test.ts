import {afterEach, describe, expect, it} from 'bun:test';
import {BoxRenderable, rgbToHex} from '@opentui/core';
import {createTestRenderer, type TestRendererSetup} from '@opentui/core/testing';
import type {AgentPhase} from '@vibesys/core-state';
import type {SessionController} from '../session-controller.js';
import {initialSessionState, type SessionState} from '../session-model.js';
import {AgentMapView, nodeLabel} from './agent-map.js';
import {resolveTheme} from './theme.js';

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
 * The selected node's backdrop: `agent-map.ts`'s `#renderGraph` and
 * `selectionBackdrop` (agent-graph.ts). `agent-map.test.ts` covers layout and
 * width; the selection-glyph tests above cover the caret. This covers the
 * pixels: the node's own interior stays plain canvas, the offset rectangle
 * lands only where `selectionBackdrop` says it can, and an edge cell inside
 * it keeps its own glyph and colour.
 */
describe("selected agent node's backdrop", () => {
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

  /** A cell's colours as lowercase hex, so they compare against theme values directly. */
  function spanAt(
    testRenderer: TestRendererSetup,
    row: number,
    col: number,
  ): {text: string; fg: string; bg: string} | undefined {
    const line = testRenderer.captureSpans().lines[row];
    if (line === undefined) return undefined;
    let cursor = 0;
    for (const span of line.spans) {
      if (col < cursor + span.width) {
        return {
          text: span.text[col - cursor] ?? '',
          fg: rgbToHex(span.fg).toLowerCase(),
          bg: rgbToHex(span.bg).toLowerCase(),
        };
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

  it('leaves the selected node itself on the plain canvas, not the backdrop colour', async () => {
    const theme = resolveTheme(null);
    const selected = await renderChain('implementer');
    const node = selected.renderer.root.findDescendantById('agent-implementer-0');
    const judge = selected.renderer.root.findDescendantById('agent-judge-0');
    expect(node).toBeInstanceOf(BoxRenderable);
    expect(judge).toBeInstanceOf(BoxRenderable);
    if (!(node instanceof BoxRenderable) || !(judge instanceof BoxRenderable)) return;

    const interior = spanAt(selected, node.y + 1, node.x + 1);
    // Same background as an ordinary, unselected node's interior: neither
    // carries a fill of its own.
    expect(interior?.bg).toBe(spanAt(selected, judge.y + 1, judge.x + 1)?.bg);
    expect(interior?.bg).not.toBe(theme.selectedSurface.toLowerCase());
  });

  it("paints the cell right of the node and the cell below it in the theme's selectedSurface", async () => {
    const theme = resolveTheme(null);
    const testRenderer = await renderChain('implementer');
    const node = testRenderer.renderer.root.findDescendantById('agent-implementer-0');
    expect(node).toBeInstanceOf(BoxRenderable);
    if (!(node instanceof BoxRenderable)) return;

    const rightOfNode = spanAt(testRenderer, node.y + 1, node.x + node.width);
    const belowNode = spanAt(testRenderer, node.y + node.height, node.x + 1);
    expect(rightOfNode?.bg).toBe(theme.selectedSurface.toLowerCase());
    expect(belowNode?.bg).toBe(theme.selectedSurface.toLowerCase());
  });

  it("leaves the cells level with the node's own top row and left column unpainted", async () => {
    const theme = resolveTheme(null);
    const testRenderer = await renderChain('implementer');
    const node = testRenderer.renderer.root.findDescendantById('agent-implementer-0');
    expect(node).toBeInstanceOf(BoxRenderable);
    if (!(node instanceof BoxRenderable)) return;

    // The offset rectangle starts one row below and one column right of the
    // node's own corner: the cell dead level with its top border, past its
    // right edge, and the cell dead level with its left border, past its
    // bottom edge, are outside it.
    const level = spanAt(testRenderer, node.y, node.x + node.width);
    const flush = spanAt(testRenderer, node.y + node.height, node.x);
    expect(level?.bg).not.toBe(theme.selectedSurface.toLowerCase());
    expect(flush?.bg).not.toBe(theme.selectedSurface.toLowerCase());
  });

  it('gives an unselected node no backdrop at all', async () => {
    const theme = resolveTheme(null);
    const testRenderer = await renderChain('implementer');
    const judge = testRenderer.renderer.root.findDescendantById('agent-judge-0');
    expect(judge).toBeInstanceOf(BoxRenderable);
    if (!(judge instanceof BoxRenderable)) return;

    const rightOfJudge = spanAt(testRenderer, judge.y + 1, judge.x + judge.width);
    const belowJudge = spanAt(testRenderer, judge.y + judge.height, judge.x + 1);
    expect(rightOfJudge?.bg).not.toBe(theme.selectedSurface.toLowerCase());
    expect(belowJudge?.bg).not.toBe(theme.selectedSurface.toLowerCase());
  });

  it("keeps an edge cell's own glyph and colour, adding only the backdrop as its background", async () => {
    const theme = resolveTheme(null);
    const selected = await renderChain('implementer');
    const unselected = await renderChain(null);
    const node = selected.renderer.root.findDescendantById('agent-implementer-0');
    expect(node).toBeInstanceOf(BoxRenderable);
    if (!(node instanceof BoxRenderable)) return;
    // Selection never resizes the graph (`selectedLabelWidth` always sizes
    // for the widest, selected label), so the same coordinates line up in
    // both renders.
    const departureRow = node.y + 1;
    const rightX = node.x + node.width;

    const withBackdrop = spanAt(selected, departureRow, rightX);
    const withoutBackdrop = spanAt(unselected, departureRow, rightX);
    // An outgoing edge leaves 'implementer' from exactly this cell.
    expect(withBackdrop?.text).not.toBe(' ');
    expect(withBackdrop?.text).toBe(withoutBackdrop?.text);
    expect(withBackdrop?.fg).toBe(withoutBackdrop?.fg);
    expect(withBackdrop?.bg).toBe(theme.selectedSurface.toLowerCase());
    expect(withoutBackdrop?.bg).not.toBe(theme.selectedSurface.toLowerCase());
  });
});
