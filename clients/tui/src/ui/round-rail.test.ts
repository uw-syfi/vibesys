import {describe, expect, test} from 'bun:test';
import {rgbToHex, type TextRenderable} from '@opentui/core';
import {createTestRenderer} from '@opentui/core/testing';
import type {HypothesisRound} from '@vibesys/backend-client';
import type {RoundSummary} from '@vibesys/core-state';
import type {SessionController} from '../session-controller.js';
import {initialSessionState, type SessionState} from '../session-model.js';
import {
  RAIL_COMPACT_WIDTH,
  RAIL_FULL_WIDTH,
  RoundRailView,
  railWindow,
  roundRailVisible,
  roundRailWidth,
} from './round-rail.js';
import {resolveTheme} from './theme.js';

function rounds(count: number): RoundSummary[] {
  return Array.from({length: count}, (_, index) => ({
    number: index + 1,
    status: 'completed' as const,
  }));
}

/** A run that owns the round view: the log is dismissed and rounds exist. */
function railState(count: number): SessionState {
  const base = initialSessionState();
  return {
    ...base,
    experimentLog: null,
    core: {...base.core, rounds: rounds(count)},
  };
}

/** Renders the rail for `state` and returns each row's text and fg colour. */
async function renderedRows(
  state: SessionState,
  rows: number,
): Promise<{text: string; fg: string}[]> {
  const {renderer} = await createTestRenderer({width: 120, height: 40});
  const view = new RoundRailView(renderer, {} as unknown as SessionController, resolveTheme(null));
  view.render(state, RAIL_FULL_WIDTH, rows);
  const rendered = view.output.getChildren().map(child => {
    const content = (child as {content?: {chunks?: {text?: string}[]}}).content;
    return {
      text: (content?.chunks ?? []).map(chunk => chunk.text ?? '').join(''),
      fg: rgbToHex((child as TextRenderable).fg).toLowerCase(),
    };
  });
  view.destroy();
  return rendered;
}

describe('railWindow', () => {
  test('shows every round when they all fit', () => {
    const view = railWindow(rounds(5), 1, 20);
    expect(view.rounds).toHaveLength(5);
    expect(view.hiddenBefore).toBe(0);
    expect(view.hiddenAfter).toBe(0);
  });

  test('keeps the selected round visible however far into the run it is', () => {
    for (const selected of [1, 2, 37, 99, 100]) {
      const view = railWindow(rounds(100), selected, 10);
      expect(view.rounds.some(round => round.number === selected)).toBe(true);
    }
  });

  test('reports what it had to hide on each side', () => {
    const view = railWindow(rounds(100), 50, 10);
    expect(view.hiddenBefore).toBeGreaterThan(0);
    expect(view.hiddenAfter).toBeGreaterThan(0);
    expect(view.hiddenBefore + view.rounds.length + view.hiddenAfter).toBe(100);
  });

  test('reserves two rows for the overflow counts when the run does not fit', () => {
    const rows = 10;
    const view = railWindow(rounds(100), 50, rows);
    // Both indicators show, so the rounds take the rows the counts do not.
    expect(view.hiddenBefore).toBeGreaterThan(0);
    expect(view.hiddenAfter).toBeGreaterThan(0);
    expect(view.rounds.length).toBe(rows - 2);
  });

  test('never exceeds the rows it was given', () => {
    for (const selected of [1, 8, 44, 100]) {
      const rows = 10;
      const view = railWindow(rounds(100), selected, rows);
      expect(view.rounds.length).toBeLessThanOrEqual(rows);
    }
  });

  test('fills the rail when the selection sits at either end', () => {
    const atStart = railWindow(rounds(100), 1, 10);
    const atEnd = railWindow(rounds(100), 100, 10);
    expect(atStart.rounds.length).toBeGreaterThan(3);
    expect(atEnd.rounds.length).toBeGreaterThan(3);
    expect(atStart.hiddenBefore).toBe(0);
    expect(atEnd.hiddenAfter).toBe(0);
  });

  test('slides by one as the selection steps, so the run scrolls rather than pages', () => {
    const all = rounds(100);
    let previous = railWindow(all, 20, 10);
    for (let selected = 21; selected < 30; selected += 1) {
      const next = railWindow(all, selected, 10);
      expect(next.rounds.some(round => round.number === selected)).toBe(true);
      // The window moves at most one round per step: no jumping.
      expect(Math.abs(next.hiddenBefore - previous.hiddenBefore)).toBeLessThanOrEqual(1);
      previous = next;
    }
  });

  test('keeps round order stable, top to bottom', () => {
    const view = railWindow(rounds(100), 50, 10);
    const numbers = view.rounds.map(round => round.number);
    expect(numbers).toEqual([...numbers].sort((a, b) => a - b));
  });

  test('handles an empty run and a one-round run', () => {
    expect(railWindow([], null, 10).rounds).toEqual([]);
    expect(railWindow(rounds(1), 1, 10).rounds).toHaveLength(1);
  });

  test('has nothing to show when it is given no rows', () => {
    const view = railWindow(rounds(10), 5, 0);
    expect(view.rounds).toEqual([]);
    expect(view.hiddenAfter).toBe(10);
  });

  test('still shows the selection in a very short rail', () => {
    const view = railWindow(rounds(100), 60, 3);
    expect(view.rounds.some(round => round.number === 60)).toBe(true);
  });

  test('never returns more rounds than a one or two row rail can hold', () => {
    for (const rows of [1, 2]) {
      const view = railWindow(rounds(100), 50, rows);
      expect(view.rounds.length).toBeLessThanOrEqual(rows);
      expect(view.rounds.length).toBeGreaterThanOrEqual(1);
      expect(view.rounds.some(round => round.number === 50)).toBe(true);
    }
  });
});

describe('roundRailWidth', () => {
  test('gives the full rail at wide terminals', () => {
    expect(roundRailWidth(120)).toBe(RAIL_FULL_WIDTH);
    expect(roundRailWidth(100)).toBe(RAIL_FULL_WIDTH);
  });

  test('falls back to the compact column between the thresholds', () => {
    expect(roundRailWidth(99)).toBe(RAIL_COMPACT_WIDTH);
    expect(roundRailWidth(85)).toBe(RAIL_COMPACT_WIDTH);
  });

  test('collapses to nothing below the narrow threshold', () => {
    // 84 columns leave the agents fallback (30) and the transcript floor (42) no
    // room beside the 13-column compact rail, so the rail hides rather than
    // squeeze the transcript under its minimum.
    expect(roundRailWidth(84)).toBe(0);
    expect(roundRailWidth(40)).toBe(0);
  });
});

describe('roundRailVisible', () => {
  test('is on for a run that owns the round view at a usable width', () => {
    expect(roundRailVisible(railState(3), 120)).toBe(true);
    expect(roundRailVisible(railState(3), 90)).toBe(true);
  });

  test('is off before the run has any rounds', () => {
    expect(roundRailVisible(railState(0), 120)).toBe(false);
  });

  test('is off while the experiment log is the landing view', () => {
    const state = {
      ...railState(3),
      experimentLog: {entries: [], selectedId: null, pending: true, error: null},
    };
    expect(roundRailVisible(state, 120)).toBe(false);
  });

  test('is off when a pane is zoomed', () => {
    const state = railState(3);
    const zoomed = {...state, layout: {...state.layout, zoomedPane: 'agents' as const}};
    expect(roundRailVisible(zoomed, 120)).toBe(false);
  });

  test('is off when a right-pane split takes the row at a fitting width', () => {
    const state = railState(3);
    const split = {
      ...state,
      layout: {
        ...state.layout,
        right: {view: 'perf' as const, title: 'Perf', content: '', pending: false, error: null},
      },
    };
    // Wide enough for the split to open, so the rail yields the row to it.
    expect(roundRailVisible(split, 120)).toBe(false);
    // Too narrow for the split but wide enough for the rail, so it keeps the row.
    expect(roundRailVisible(split, 90)).toBe(true);
  });

  test('is off below the collapse width even for a live run', () => {
    expect(roundRailVisible(railState(3), 84)).toBe(false);
    expect(roundRailVisible(railState(3), 60)).toBe(false);
  });
});

describe('RoundRailView row budget', () => {
  async function railChildren(count: number, rows: number, selected: number): Promise<string[]> {
    const rendered = await renderedRows({...railState(count), selectedRound: selected}, rows);
    return rendered.map(row => row.text);
  }

  test('draws nothing when the box has no content rows', async () => {
    // rows minus the two border rows leaves no room for a round or an indicator.
    expect(await railChildren(100, 2, 50)).toHaveLength(0);
    expect(await railChildren(100, 1, 50)).toHaveLength(0);
  });

  test('never emits more children than the content rows, dropping indicators first', async () => {
    // One content row with the selection buried mid-run: the round wins the row
    // and neither overflow indicator is drawn, because there is no row to spare.
    const one = await railChildren(100, 3, 50);
    expect(one).toHaveLength(1);
    expect(one.some(line => line.includes('r50'))).toBe(true);
    expect(one.some(line => line.startsWith('↑') || line.startsWith('↓'))).toBe(false);

    // Two content rows: the round keeps one, a single indicator takes the other.
    const two = await railChildren(100, 4, 50);
    expect(two).toHaveLength(2);
    expect(two.filter(line => line.startsWith('↑') || line.startsWith('↓'))).toHaveLength(1);
  });
});

describe('RoundRailView elapsed timer refresh', () => {
  /** A single active round with a live agent, so the rail arms the elapsed timer. */
  function activeRoundState(): SessionState {
    const base = railState(1);
    return {
      ...base,
      selectedRound: 1,
      core: {
        ...base.core,
        rounds: [
          {
            number: 1,
            status: 'active',
            startedAt: new Date().toISOString(),
            activeAgentStarts: {worker: new Date().toISOString()},
          },
        ],
      },
    };
  }

  function textOf(text: TextRenderable): string {
    const content = (text.content as {chunks?: {text?: string}[]} | undefined)?.chunks ?? [];
    return content.map(chunk => chunk.text ?? '').join('');
  }

  test('keeps the compact label after the elapsed timer refreshes at a compact width', async () => {
    const {renderer} = await createTestRenderer({width: 120, height: 40});
    const view = new RoundRailView(
      renderer,
      {} as unknown as SessionController,
      resolveTheme(null),
    );
    view.render(activeRoundState(), RAIL_COMPACT_WIDTH, 10);
    // The elapsed timer ticks on a real one-second interval; wait past a tick so the
    // refresh runs, then read the row it rewrote.
    await new Promise(resolve => setTimeout(resolve, 1100));
    const text = textOf(view.output.getChildren()[0] as TextRenderable);
    view.destroy();
    expect(text).not.toContain(' run ');
    expect(text.length).toBeLessThanOrEqual(RAIL_COMPACT_WIDTH);
  });

  test('keeps the elapsed suffix after the timer refreshes at full width', async () => {
    const {renderer} = await createTestRenderer({width: 120, height: 40});
    const view = new RoundRailView(
      renderer,
      {} as unknown as SessionController,
      resolveTheme(null),
    );
    view.render(activeRoundState(), RAIL_FULL_WIDTH, 10);
    await new Promise(resolve => setTimeout(resolve, 1100));
    const text = textOf(view.output.getChildren()[0] as TextRenderable);
    view.destroy();
    expect(text).toContain(' run ');
  });
});

describe('RoundRailView profile-skipped rounds', () => {
  test('marks a completed profile-skipped round hollow and dim', async () => {
    const base = railState(3);
    const state: SessionState = {
      ...base,
      selectedRound: 3,
      core: {
        ...base.core,
        rounds: [
          {number: 1, status: 'completed'},
          {number: 2, status: 'completed', profileSkipped: true},
          {number: 3, status: 'completed'},
        ],
      },
    };
    const rows = await renderedRows(state, 10);
    const theme = resolveTheme(null);

    const skipped = rows.find(row => row.text.includes('r2'));
    expect(skipped?.text).toContain('○');
    expect(skipped?.text).not.toContain('✓');
    expect(skipped?.fg).toBe(theme.textSubtle.toLowerCase());

    // A freshly measured round keeps the solid check and the primary colour.
    const fresh = rows.find(row => row.text.includes('r1'));
    expect(fresh?.text).toContain('✓');
    expect(fresh?.text).not.toContain('○');
    expect(fresh?.fg).toBe(theme.textPrimary.toLowerCase());
  });

  test('keeps the failure cross on a failed round that also skipped profiling', async () => {
    const base = railState(2);
    const state: SessionState = {
      ...base,
      selectedRound: 1,
      core: {
        ...base.core,
        rounds: [
          {number: 1, status: 'completed'},
          // How the round ended outranks how it measured: no hollow ring here.
          {number: 2, status: 'failed', profileSkipped: true},
        ],
      },
    };
    const rows = await renderedRows(state, 10);

    const failed = rows.find(row => row.text.includes('r2'));
    expect(failed?.text).toContain('✗');
    expect(failed?.text).not.toContain('○');
  });
});

describe('RoundRailView judge verdict', () => {
  /** A single completed round whose experiment-log record carries `verdict`. */
  function verdictState(verdict: 'pass' | 'fail' | 'deferred' | null | undefined): SessionState {
    const base = railState(1);
    const record: HypothesisRound = {
      round: 1,
      passed: verdict === 'pass',
      reviewed: true,
      // Omitted rather than set to `undefined`: a round the judge has not
      // reached yet has no `judge_verdict` key at all, same as the backend
      // sends it (exactOptionalPropertyTypes forbids the key set to
      // `undefined` explicitly).
      ...(verdict !== undefined ? {judge_verdict: verdict} : {}),
    };
    return {
      ...base,
      selectedRound: 1,
      experimentLog: {
        entries: [{hypothesis_id: 'H-01', first_round: 1, last_round: 1, rounds: [record]}],
        selectedId: null,
        pending: false,
        error: null,
      },
    };
  }

  /** The row for round 1 as its plain text, or undefined once `view.render` ran. */
  function rowText(view: RoundRailView): string | undefined {
    return view.output
      .getChildren()
      .map(child => {
        const content = (child as {content?: {chunks?: {text?: string}[]}}).content;
        return (content?.chunks ?? []).map(chunk => chunk.text ?? '').join('');
      })
      .find(line => line.includes('r1'));
  }

  test('marks a completed round the judge failed, without losing the round-status check', async () => {
    const rows = await renderedRows(verdictState('fail'), 10);
    const row = rows.find(r => r.text.includes('r1'));
    // The verdict replaces the status glyph rather than sitting beside it: a
    // row carrying both a check and a cross reads as two contradictory claims,
    // and the status word ('done') already says the round finished.
    expect(row?.text).toContain('✗');
    expect(row?.text).not.toContain('✓');
    expect(row?.text).toContain('done');
  });

  test('adds no mark for a pass verdict, a deferred one, or a round not yet judged', async () => {
    // One renderer reused across every verdict, so the run's assertions read as
    // one behaviour (fail is the only mark-worthy case) rather than four
    // independent renders.
    const {renderer} = await createTestRenderer({width: 120, height: 40});
    const view = new RoundRailView(
      renderer,
      {} as unknown as SessionController,
      resolveTheme(null),
    );
    for (const verdict of ['pass', 'deferred', null, undefined] as const) {
      view.render(verdictState(verdict), RAIL_FULL_WIDTH, 10);
      expect(rowText(view)).not.toContain('✗');
    }
    view.destroy();
  });

  test('carries the verdict into the compact row, which keeps only the glyph', async () => {
    const {renderer} = await createTestRenderer({width: 120, height: 40});
    const view = new RoundRailView(
      renderer,
      {} as unknown as SessionController,
      resolveTheme(null),
    );
    view.render(verdictState('fail'), RAIL_COMPACT_WIDTH, 10);
    const content = (
      view.output.getChildren()[0] as unknown as {
        content?: {chunks?: {text?: string}[]};
      }
    ).content;
    const text = (content?.chunks ?? []).map(chunk => chunk.text ?? '').join('');
    view.destroy();
    // Compact keeps nothing past the glyph, which is exactly why the verdict
    // lives in the glyph: a narrow rail is where an operator can least afford
    // to lose the one fact that a finished round was still judged a failure.
    expect(text).toBe('▸r1✗');
  });

  test('keeps a fail verdict inside the usable width even at a high round count', async () => {
    const base = railState(1);
    const state: SessionState = {
      ...base,
      selectedRound: 999,
      core: {...base.core, rounds: [{number: 999, status: 'completed'}]},
      experimentLog: {
        entries: [
          {
            hypothesis_id: 'H-01',
            first_round: 999,
            last_round: 999,
            rounds: [
              {
                round: 999,
                passed: false,
                reviewed: true,
                judge_verdict: 'fail',
                perf_delta_pct: -100,
              },
            ],
          },
        ],
        selectedId: null,
        pending: false,
        error: null,
      },
    };
    const rows = await renderedRows(state, 10);
    const row = rows.find(r => r.text.includes('r999'));
    // RAIL_FULL_WIDTH (28) minus the border (2) and the 1-column padding on
    // each side (2) leaves 24 usable columns.
    expect(row?.text.length).toBeLessThanOrEqual(RAIL_FULL_WIDTH - 4);
  });
});
