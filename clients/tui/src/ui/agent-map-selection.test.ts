import {afterEach, describe, expect, it} from 'bun:test';
import {createTestRenderer} from '@opentui/core/testing';
import type {AgentPhase} from '@vibesys/core-state';
import type {SessionController} from '../session-controller.js';
import {initialSessionState, type SessionState} from '../session-model.js';
import {SPINNER_FRAMES} from './activity-bar.js';
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
    // Active draws the shared spinner's frame 0 rather than a static marker;
    // see the `nodeLabel spinner frame` suite in agent-map-spinner.test.ts.
    expect(nodeLabel(phase('active'), false)).toBe(`${SPINNER_FRAMES[0]} implementer`);
    expect(nodeLabel(phase('active'), true)).toBe(`› ${SPINNER_FRAMES[0]} implementer`);
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

    expect(frame).toContain(`› ${SPINNER_FRAMES[0]} implementer`);
    expect(frame).toContain('○ judge');
    expect(frame).not.toContain('› ○ judge');
  });
});
