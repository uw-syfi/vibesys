import {afterEach, describe, expect, it, spyOn} from 'bun:test';
import type {Renderable} from '@opentui/core';
import {createTestRenderer} from '@opentui/core/testing';
import type {AgentPhase} from '@vibesys/core-state';
import type {SessionController} from '../session-controller.js';
import {initialSessionState, type SessionState} from '../session-model.js';
import {SPINNER_FRAMES, SPINNER_INTERVAL_MS} from './activity-bar.js';
import {AgentMapView, nodeLabel} from './agent-map.js';
import {resolveTheme} from './theme.js';

// Kept apart from agent-map.test.ts and agent-map-selection.test.ts for the
// same reason those two are split: unrelated changes to either suite should
// not textually conflict with these spinner-specific tests.

/**
 * `view.output` itself never changes: it holds one child (`#content`) for the
 * view's whole life, so checking only the top level would prove nothing about
 * whether `#clear()` tore down and rebuilt everything underneath it. Walking
 * every descendant is what actually catches a full rebuild: `#clear()`
 * destroys and recreates the heading, the canvas, every edge run and every
 * node box, so any one of those changing identity means a rebuild happened.
 */
function descendants(node: Renderable): Renderable[] {
  return [node, ...node.getChildren().flatMap(descendants)];
}

/**
 * Before this, an active node's marker was the static `●`, same as every
 * other status's marker was its own fixed glyph. `nodeLabel` now draws the
 * shared `SPINNER_FRAMES` animation in that one cell instead, so "this is
 * running right now" is visible the way the activity bar and the chat
 * composer already show it.
 */
describe('nodeLabel spinner frame', () => {
  function phase(status: AgentPhase['status']): AgentPhase {
    return {kind: 'implementer', status, roundNumber: null, roundLabel: null};
  }

  it('draws the current spinner frame for an active phase, not a static marker', () => {
    expect(nodeLabel(phase('active'), false, 0)).toBe(`${SPINNER_FRAMES[0]} implementer`);
    expect(nodeLabel(phase('active'), false, 1)).toBe(`${SPINNER_FRAMES[1]} implementer`);
    expect(nodeLabel(phase('active'), false, 0)).not.toBe(nodeLabel(phase('active'), false, 1));
  });

  it('wraps the frame index the way the activity bar and chat composer do', () => {
    expect(nodeLabel(phase('active'), false, SPINNER_FRAMES.length)).toBe(
      nodeLabel(phase('active'), false, 0),
    );
  });

  it('leaves every non-active status on its own static glyph, whatever the frame', () => {
    for (const status of ['pending', 'completed', 'failed', 'cancelled', 'interrupted'] as const) {
      expect(nodeLabel(phase(status), false, 3)).toBe(nodeLabel(phase(status), false, 0));
    }
  });

  it('keeps the marker cell one column wide, static or animated', () => {
    const width = (label: string): number => [...label].length;
    const pendingWidth = width(nodeLabel(phase('pending'), false));
    for (let frame = 0; frame < SPINNER_FRAMES.length; frame += 1) {
      expect(width(nodeLabel(phase('active'), false, frame))).toBe(pendingWidth);
    }
  });
});

describe('agent node spinner animation', () => {
  const cleanup: Array<() => void> = [];

  afterEach(() => {
    for (const destroy of cleanup.splice(0).reverse()) destroy();
  });

  /** Never fires in a render-only test; onMouseUp is never simulated. */
  const controller = {
    focusRound: () => {},
    selectAgent: () => {},
  } as unknown as SessionController;

  function stateWith(phases: AgentPhase[]): SessionState {
    const base = initialSessionState();
    return {...base, core: {...base.core, phases}, selectedAgentKind: null};
  }

  it('animates only the active graph node, in place, without rebuilding the tree', async () => {
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
    view.render(stateWith(phases), 60);
    await testRenderer.renderOnce();
    const before = testRenderer.captureCharFrame();
    const beforeTree = descendants(view.output);

    // A comfortable margin past one 120ms tick, well short of a full ten-frame
    // wrap (1200ms), so the frame is guaranteed to differ without pinning
    // exactly which one it lands on.
    await new Promise(resolve => setTimeout(resolve, SPINNER_INTERVAL_MS + 130));
    await testRenderer.renderOnce();
    const after = testRenderer.captureCharFrame();
    const afterTree = descendants(view.output);

    const spinnerClass = `[${SPINNER_FRAMES.join('')}]`;
    expect(before).toMatch(new RegExp(`${spinnerClass} implementer`));
    expect(after).toMatch(new RegExp(`${spinnerClass} implementer`));
    expect(before).not.toBe(after);
    // The static neighbour never moved or changed.
    expect(after).toContain('○ judge');
    // Nothing widened: both frames are the same terminal, same row count.
    expect(after.split('\n').map(row => row.length)).toEqual(
      before.split('\n').map(row => row.length),
    );
    // No full `render()` rebuild happened: every renderable in the tree (the
    // heading, the canvas, every edge run, every node box and its three text
    // lines) kept its identity across the tick. `#clear()` destroys and
    // recreates all of them on a real rebuild, so reference equality here,
    // node for node, is proof one did not happen.
    expect(afterTree.length).toBe(beforeTree.length);
    expect(afterTree.length).toBeGreaterThan(5);
    for (const [index, node] of beforeTree.entries()) {
      expect(afterTree[index]).toBe(node);
    }
  });

  it('animates the stacked fallback the same way, for a terminal too narrow for the graph', async () => {
    const testRenderer = await createTestRenderer({width: 50, height: 24});
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
    view.render(stateWith(phases));
    await testRenderer.renderOnce();
    const before = testRenderer.captureCharFrame();
    // The stacked fallback's own tell: the down arrow between two phases,
    // which the graph layout never draws.
    expect(before).toContain('↓');

    await new Promise(resolve => setTimeout(resolve, SPINNER_INTERVAL_MS + 130));
    await testRenderer.renderOnce();
    const after = testRenderer.captureCharFrame();

    const spinnerClass = `[${SPINNER_FRAMES.join('')}]`;
    expect(before).toMatch(new RegExp(`${spinnerClass} implementer`));
    expect(before).not.toBe(after);
    expect(after).toContain('○ judge');
  });

  it('runs no spinner timer while nothing is active, starts one only when a phase is, and clears it', async () => {
    const setIntervalSpy = spyOn(globalThis, 'setInterval');
    const clearIntervalSpy = spyOn(globalThis, 'clearInterval');
    const renderer = await createTestRenderer({width: 100, height: 24});
    const view = new AgentMapView(renderer.renderer, controller, resolveTheme(null));
    renderer.renderer.root.add(view.output);
    cleanup.push(() => {
      view.output.destroyRecursively();
      renderer.renderer.destroy();
      setIntervalSpy.mockRestore();
      clearIntervalSpy.mockRestore();
    });

    const idle: AgentPhase[] = [
      {kind: 'implementer', status: 'completed', roundNumber: null, roundLabel: null},
    ];
    view.render(stateWith(idle), 60);
    expect(setIntervalSpy).not.toHaveBeenCalled();

    const active: AgentPhase[] = [
      {kind: 'implementer', status: 'active', roundNumber: null, roundLabel: null},
    ];
    view.render(stateWith(active), 60);
    expect(setIntervalSpy).toHaveBeenCalledTimes(1);

    // A fresh state object forces a real repaint, the way a live update would.
    view.render(stateWith(idle), 61);
    expect(clearIntervalSpy).toHaveBeenCalledTimes(1);

    view.render(stateWith(active), 62);
    expect(setIntervalSpy).toHaveBeenCalledTimes(2);
    view.destroy();
    expect(clearIntervalSpy).toHaveBeenCalledTimes(2);
  });
});
