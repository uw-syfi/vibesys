import {describe, expect, test} from 'bun:test';
import {BoxRenderable, rgbToHex, TextAttributes} from '@opentui/core';
import {createTestRenderer, type TestRendererSetup} from '@opentui/core/testing';
import type {HypothesisRound} from '@vibesys/backend-client';
import type {RoundState} from '@vibesys/core-state';
import type {SessionController} from '../session-controller.js';
import {initialSessionState, type SessionState} from '../session-model.js';
import {RoundTabsView, roundTab, tabWindow} from './round-tabs.js';
import {contrastRatio, listThemes, resolveTheme, type Theme} from './theme.js';

const secondsAgo = (seconds: number): string => new Date(Date.now() - seconds * 1000).toISOString();
const done = (number: number): RoundState => ({number, status: 'completed'});
/** An active round whose agent started `seconds` ago. Half seconds keep the label off a tick. */
const live = (number: number, seconds: number): RoundState => ({
  number,
  status: 'active',
  activeAgentStarts: {worker: secondsAgo(seconds)},
});
const measured = (round: number, delta: number): HypothesisRound => ({
  round,
  passed: true,
  reviewed: true,
  perf_delta_pct: delta,
});
const judged = (round: number, verdict: 'pass' | 'fail' | 'deferred' | null): HypothesisRound => ({
  round,
  passed: verdict === 'pass',
  reviewed: true,
  judge_verdict: verdict,
});

function runState(
  rounds: RoundState[],
  options: {selected: number | null; maxRounds?: number; records?: HypothesisRound[]},
): SessionState {
  const base = initialSessionState();
  return {
    ...base,
    selectedRound: options.selected,
    core: {...base.core, rounds, maxRounds: options.maxRounds ?? null},
    experimentLog: {
      entries: [
        {
          hypothesis_id: 'H-01',
          first_round: 1,
          last_round: rounds.length,
          rounds: options.records ?? [],
        },
      ],
      selectedId: null,
      pending: false,
      error: null,
    },
  };
}

/** One of each outcome; r7 is planned through `maxRounds`. */
function rolesState(selected: number): SessionState {
  return runState(
    [
      done(1),
      done(2),
      done(3),
      {number: 4, status: 'failed', profileSkipped: true},
      {number: 5, status: 'completed', profileSkipped: true},
      live(6, 42.5),
    ],
    {selected, maxRounds: 7, records: [measured(1, 12), measured(2, 1.5), judged(3, 'fail')]},
  );
}

/** r1 selected at the far end from the live r4, so every tab between them has to fit. */
function ladderState(): SessionState {
  return runState([done(1), done(2), done(3), live(4, 42.5)], {
    selected: 1,
    records: [measured(1, 12), measured(2, 1.5), measured(3, -3)],
  });
}

interface Cell {
  ch: string;
  fg: string;
  bg: string;
  bold: boolean;
}

interface Bar {
  setup: TestRendererSetup;
  view: RoundTabsView;
  rows: number;
  cells: Cell[];
}

function rowCells(setup: TestRendererSetup): Cell[] {
  const cells: Cell[] = [];
  for (const span of setup.captureSpans().lines[0]?.spans ?? []) {
    for (const ch of span.text) {
      cells.push({
        ch,
        fg: rgbToHex(span.fg).toLowerCase(),
        bg: rgbToHex(span.bg).toLowerCase(),
        bold: (span.attributes & TextAttributes.BOLD) !== 0,
      });
    }
  }
  return cells;
}

const rowText = (cells: Cell[]): string =>
  cells
    .map(cell => cell.ch)
    .join('')
    .trimEnd();

/** The cells the first occurrence of `needle` occupies. */
function cellsOf(cells: Cell[], needle: string): Cell[] {
  const start = cells
    .map(cell => cell.ch)
    .join('')
    .indexOf(needle);
  expect(start).toBeGreaterThanOrEqual(0);
  return cells.slice(start, start + [...needle].length);
}

const fgOf = (cells: Cell[], needle: string): string[] => [
  ...new Set(cellsOf(cells, needle).map(cell => cell.fg)),
];

async function renderBar(
  state: SessionState,
  width: number,
  options: {theme?: Theme; controller?: Partial<SessionController>} = {},
): Promise<Bar> {
  const theme = options.theme ?? resolveTheme(null);
  const setup = await createTestRenderer({width, height: 2});
  // The canvas the bar sits on in the app; the bar itself paints only the selected slot.
  const backdrop = new BoxRenderable(setup.renderer, {
    width: '100%',
    height: '100%',
    backgroundColor: theme.canvas,
  });
  const view = new RoundTabsView(
    setup.renderer,
    (options.controller ?? {}) as SessionController,
    theme,
  );
  backdrop.add(view.output);
  setup.renderer.root.add(backdrop);
  const rows = view.render(state, width);
  await setup.renderOnce();
  return {setup, view, rows, cells: rowCells(setup)};
}

async function redraw(bar: Bar, state: SessionState, width: number): Promise<Cell[]> {
  bar.view.render(state, width);
  await bar.setup.renderOnce();
  return rowCells(bar.setup);
}

function close(bar: Bar): void {
  bar.view.destroy();
  bar.setup.renderer.destroy();
}

const eight = (count: number): number[] => Array.from({length: count}, () => 8);

describe('tabWindow', () => {
  test('shows every slot when they all fit, and windows once they do not', () => {
    // 3 slots of 8 and 2 gaps are 26 columns; the margin and the right edge take 2.
    expect(tabWindow(eight(3), 1, null, 28)).toEqual({first: 0, last: 2});
    expect(tabWindow(eight(3), 1, null, 27)).toEqual({first: 1, last: 2});
  });

  test('grows from the selection, taking the later side on a tie', () => {
    expect(tabWindow(eight(10), 5, null, 40)).toEqual({first: 4, last: 6});
  });

  test('fills toward the only side left when the selection sits at either end', () => {
    expect(tabWindow(eight(10), 0, null, 40)).toEqual({first: 0, last: 2});
    expect(tabWindow(eight(10), 9, null, 40)).toEqual({first: 7, last: 9});
  });

  test('reserves a marker only for a side that is still hidden', () => {
    // Four slots leave one hidden (`1 ›` plus 2 spaces = 5): 35 + 5 = 40 <= 43.
    // Reserving both markers up front would stop at three.
    expect(tabWindow(eight(5), 0, null, 45)).toEqual({first: 0, last: 3});
  });

  test("reserves a marker's real width, two-digit counts included", () => {
    // `‹ 6` + 2 is 5 columns and lets a fourth slot in; `‹ 26` + 2 is 6 and does not.
    expect(tabWindow(eight(10), 9, null, 42)).toEqual({first: 6, last: 9});
    expect(tabWindow(eight(30), 29, null, 42)).toEqual({first: 27, last: 29});
  });

  test('grows toward the live round until it is in, then outward', () => {
    expect(tabWindow(eight(30), 2, 12, 120)).toEqual({first: 1, last: 12});
    expect(tabWindow(eight(30), 25, 15, 120)).toEqual({first: 15, last: 26});
  });

  test('has no window when the live round cannot be reached from the selection', () => {
    expect(tabWindow(eight(30), 2, 20, 120)).toBeNull();
  });

  test('has no window when the selected slot alone does not fit', () => {
    expect(tabWindow(eight(3), 1, null, 12)).toBeNull();
  });
});

describe('roundTab', () => {
  const now = new Date();

  test('shows the measured delta once a round resolves', () => {
    const state = runState([done(1), done(2), done(3)], {
      selected: 1,
      records: [measured(1, 12), measured(2, 1.5), measured(3, -3)],
    });
    expect(state.core.rounds.map(round => roundTab(round, state, now))).toEqual([
      {number: 1, outcome: 'done', metric: '+12%'},
      {number: 2, outcome: 'done', metric: '+1.5%'},
      {number: 3, outcome: 'done', metric: '-3.0%'},
    ]);
  });

  test('shows the wall duration of a round with no measured delta', () => {
    const round: RoundState = {
      number: 1,
      status: 'completed',
      finishedAt: '2026-01-01T00:01:23.000Z',
      agentIntervals: [
        {startedAt: '2026-01-01T00:00:00.000Z', finishedAt: '2026-01-01T00:01:23.000Z'},
      ],
    };
    const state = runState([round], {selected: 1});
    expect(roundTab(round, state, now)).toEqual({number: 1, outcome: 'done', metric: '1m 23s'});
  });

  test('says fail for a round the judge failed, whatever it measured', () => {
    const state = runState([done(1)], {
      selected: 1,
      records: [{...judged(1, 'fail'), perf_delta_pct: -100}],
    });
    expect(roundTab(done(1), state, now)).toEqual({number: 1, outcome: 'fail', metric: 'fail'});
  });

  test('keeps the check for a pass verdict, a deferred one, or a round not yet judged', () => {
    for (const verdict of ['pass', 'deferred', null] as const) {
      const state = runState([done(1)], {selected: 1, records: [judged(1, verdict)]});
      expect(roundTab(done(1), state, now).outcome).toBe('done');
    }
    const unjudged = runState([done(1)], {
      selected: 1,
      records: [{round: 1, passed: false, reviewed: false}],
    });
    expect(roundTab(done(1), unjudged, now).outcome).toBe('done');
  });

  test('says fail for a failed round even when it also skipped profiling', () => {
    const round: RoundState = {number: 1, status: 'failed', profileSkipped: true};
    expect(roundTab(round, runState([round], {selected: 1}), now)).toEqual({
      number: 1,
      outcome: 'fail',
      metric: 'fail',
    });
  });

  test('says skipped for a completed round that skipped profiling', () => {
    const round: RoundState = {number: 1, status: 'completed', profileSkipped: true};
    expect(roundTab(round, runState([round], {selected: 1}), now)).toEqual({
      number: 1,
      outcome: 'skipped',
      metric: 'skipped',
    });
  });

  test('counts a live round up from its agent start, and leaves a planned one bare', () => {
    const running = live(1, 42.5);
    const planned: RoundState = {number: 2, status: 'planned'};
    const state = runState([running, planned], {selected: 1});
    expect(roundTab(running, state, new Date())).toEqual({
      number: 1,
      outcome: 'live',
      metric: '42s',
    });
    expect(roundTab(planned, state, now)).toEqual({number: 2, outcome: 'planned', metric: ''});
  });
});

describe('RoundTabsView', () => {
  const theme = resolveTheme(null);

  test('draws each outcome with its glyph and metric in its status colour', async () => {
    const bar = await renderBar(rolesState(2), 120);
    const {cells} = bar;
    close(bar);

    expect(rowText(cells)).toBe(
      '   r1 ✓ +12%   ▎ r2 ✓ +1.5%     r3 ✗ fail     r4 ✗ fail     r5 ○ skipped     r6 ⟳ 42s     r7 ·',
    );
    expect(fgOf(cells, 'r1')).toEqual([theme.textMuted]);
    expect(fgOf(cells, '✓')).toEqual([theme.success]);
    expect(fgOf(cells, '+12%')).toEqual([theme.textMuted]);
    expect(fgOf(cells, 'r3')).toEqual([theme.textMuted]);
    expect(fgOf(cells, '✗ fail')).toEqual([theme.error]);
    expect(fgOf(cells, 'r4')).toEqual([theme.textMuted]);
    for (const part of ['r5', '○', 'skipped', 'r7', '·']) {
      expect(fgOf(cells, part)).toEqual([theme.textSubtle]);
    }
    expect(fgOf(cells, 'r6')).toEqual([theme.textMuted]);
    expect(fgOf(cells, '⟳')).toEqual([theme.warning]);
    expect(fgOf(cells, '42s')).toEqual([theme.warning]);
    // Only the selected slot has a fill; everything else is the canvas.
    // From the gap after the selected slot through r4, and the margin before r1.
    const unselected = [...cells.slice(0, 3), ...cellsOf(cells, '   r3 ✗ fail     r4')];
    expect(new Set(unselected.map(cell => cell.bg))).toEqual(new Set([theme.canvas]));
    expect(cells.filter(cell => cell.bold).map(cell => cell.ch)).toEqual(['r', '2', '✓']);
  });

  test('fills the whole selected slot and marks its leading edge', async () => {
    const bar = await renderBar(rolesState(2), 120);
    const {cells} = bar;
    close(bar);

    const slot = cellsOf(cells, '▎ r2 ✓ +1.5%  ');
    expect(new Set(slot.map(cell => cell.bg))).toEqual(new Set([theme.selectedSurface]));
    const start = cells.indexOf(slot[0] as Cell);
    expect(cells[start - 1]?.bg).toBe(theme.canvas);
    expect(cells[start + slot.length]?.bg).toBe(theme.canvas);
    expect(fgOf(cells, '▎')).toEqual([theme.accent]);
    expect(fgOf(cells, 'r2')).toEqual([theme.textStrong]);
    expect(cellsOf(cells, '▎ r2 ✓')[5]).toMatchObject({fg: theme.success, bold: true});
    expect(fgOf(cells, '+1.5%')).toEqual([theme.textPrimary]);
  });

  test('shows the selected round and the live round together when they differ', async () => {
    const bar = await renderBar(rolesState(2), 120);
    const {cells} = bar;
    close(bar);

    // r2 carries the selection, r6 the live state, each unmistakably.
    expect(cellsOf(cells, 'r2')[0]?.bg).toBe(theme.selectedSurface);
    expect(cellsOf(cells, 'r6 ⟳ 42s').map(cell => cell.bg)).not.toContain(theme.selectedSurface);
    expect(fgOf(cells, '⟳ 42s')).toEqual([theme.warning]);
  });

  test('keeps the live colours on a selected live round', async () => {
    const bar = await renderBar(rolesState(6), 120);
    const {cells} = bar;
    close(bar);

    const slot = cellsOf(cells, '▎ r6 ⟳ 42s  ');
    expect(new Set(slot.map(cell => cell.bg))).toEqual(new Set([theme.selectedSurface]));
    expect(cellsOf(cells, 'r6')[0]).toMatchObject({fg: theme.textStrong, bold: true});
    expect(cellsOf(cells, '⟳')[0]).toMatchObject({fg: theme.warning, bold: true});
    expect(fgOf(cells, '42s')).toEqual([theme.warning]);
    // r2 is back to an ordinary done tab.
    expect(fgOf(cells, '+1.5%')).toEqual([theme.textMuted]);
    expect(cellsOf(cells, 'r2')[0]?.bg).toBe(theme.canvas);
  });

  test('keeps every colour on the fill readable, in every theme', async () => {
    const failures: string[] = [];
    for (const each of listThemes()) {
      const bar = await renderBar(rolesState(1), 120, {theme: each});
      for (let selected = 1; selected <= 7; selected += 1) {
        const cells = await redraw(bar, rolesState(selected), 120);
        for (const cell of cells) {
          if (cell.bg !== each.selectedSurface.toLowerCase() || cell.ch.trim() === '') continue;
          const ratio = contrastRatio(cell.fg, each.selectedSurface);
          if (ratio < each.minContrast) {
            failures.push(`${each.name} r${selected} '${cell.ch}' ${ratio.toFixed(2)}`);
          }
        }
      }
      close(bar);
    }
    expect(failures).toEqual([]);
  });

  test('keeps the selection in view at either end and in the middle, with exact counts', async () => {
    const bar = await renderBar(runState([], {selected: 10, maxRounds: 20}), 40);
    const middle = rowText(bar.cells);
    const first = rowText(await redraw(bar, runState([], {selected: 1, maxRounds: 20}), 40));
    const last = rowText(await redraw(bar, runState([], {selected: 20, maxRounds: 20}), 40));
    const markers = await redraw(bar, runState([], {selected: 10, maxRounds: 20}), 40);
    close(bar);

    expect(middle).toBe(' ‹ 8    r9 ·   ▎ r10 ·     r11 ·    9 ›');
    expect(first).toBe(' ▎ r1 ·     r2 ·     r3 ·    17 ›');
    expect(last).toBe(' ‹ 17    r18 ·     r19 ·   ▎ r20 ·');
    expect(fgOf(markers, '‹ 8')).toEqual([theme.textSubtle]);
    expect(fgOf(markers, '9 ›')).toEqual([theme.textSubtle]);
  });

  test('degrades in the ladder order as the width narrows, at each boundary', async () => {
    const bar = await renderBar(ladderState(), 58);
    const rows: [number, string][] = [];
    for (const width of [58, 57, 46, 45, 38, 37, 33, 32, 28, 27, 12, 11, 4]) {
      rows.push([width, rowText(await redraw(bar, ladderState(), width))]);
    }
    close(bar);

    expect(rows).toEqual([
      // L0: everything fits.
      [58, ' ▎ r1 ✓ +12%     r2 ✓ +1.5%     r3 ✓ -3.0%     r4 ⟳ 42s'],
      // L1: tabs that are neither selected nor live lose their metric.
      [57, ' ▎ r1 ✓ +12%     r2 ✓     r3 ✓     r4 ⟳ 42s'],
      [46, ' ▎ r1 ✓ +12%     r2 ✓     r3 ✓     r4 ⟳ 42s'],
      // L2: padding 2 -> 1.
      [45, ' ▎r1 ✓ +12%   r2 ✓   r3 ✓   r4 ⟳ 42s'],
      [38, ' ▎r1 ✓ +12%   r2 ✓   r3 ✓   r4 ⟳ 42s'],
      // L3: the selected tab loses its metric; the live one keeps its timer.
      [37, ' ▎r1 ✓   r2 ✓   r3 ✓   r4 ⟳ 42s'],
      [33, ' ▎r1 ✓   r2 ✓   r3 ✓   r4 ⟳ 42s'],
      // L4: no inner space.
      [32, ' ▎r1✓   r2✓   r3✓   r4⟳42s'],
      [28, ' ▎r1✓   r2✓   r3✓   r4⟳42s'],
      // L4 cannot hold both: the selected round wins and the live one goes.
      [27, ' ▎r1✓   r2✓   r3✓   1 ›'],
      [12, ' ▎r1✓   3 ›'],
      // No room for a marker: the selected slot alone, clipped.
      [11, ' ▎r1✓'],
      [4, ' ▎r1'],
    ]);
  });

  test('takes no row before the run has any rounds', async () => {
    const bar = await renderBar(runState([], {selected: null}), 40);
    const empty = {rows: bar.rows, text: rowText(bar.cells)};
    const rows = bar.view.render(ladderState(), 40);
    close(bar);

    expect(empty).toEqual({rows: 0, text: ''});
    expect(rows).toBe(1);
  });

  test('draws again after `hide`, even for the state and width it last drew', async () => {
    // A resize can take a split off screen and bring the tabs back with the
    // state unchanged, so `hide` must not leave a memo that skips that draw.
    const state = ladderState();
    const bar = await renderBar(state, 58);
    bar.view.hide();
    const hidden = bar.view.output.visible;
    const rows = bar.view.render(state, 58);
    const shown = bar.view.output.visible;
    close(bar);

    expect({hidden, rows, shown}).toEqual({hidden: false, rows: 1, shown: true});
  });

  test('selects the round whose tab is clicked', async () => {
    const calls: unknown[][] = [];
    const bar = await renderBar(ladderState(), 58, {
      controller: {
        focusRound: focus => calls.push(['focusRound', focus]),
        selectRound: round => calls.push(['selectRound', round]),
      },
    });
    const column = bar.cells
      .map(cell => cell.ch)
      .join('')
      .indexOf('r3');
    await bar.setup.mockMouse.click(column, 0);
    close(bar);

    // The bar is not a pane, so a click moves no focus: it only picks the round.
    expect(calls).toEqual([['selectRound', 3]]);
  });

  test('re-lays the bar out as the live timer ticks', async () => {
    const state = runState([live(1, 9.5)], {selected: 2, maxRounds: 2});
    const bar = await renderBar(state, 40);
    const before = rowText(bar.cells);
    // The timer ticks on a real one-second interval; wait past one tick.
    await new Promise(resolve => setTimeout(resolve, 1100));
    await bar.setup.renderOnce();
    const after = rowText(rowCells(bar.setup));
    close(bar);

    expect(before).toBe('   r1 ⟳ 9s   ▎ r2 ·');
    // 9s -> 10s widens r1's slot, so the selected slot moves one column right.
    expect(after).toBe('   r1 ⟳ 10s   ▎ r2 ·');
  });

  test('repaints in a new theme on the next render', async () => {
    const light = resolveTheme('light');
    // The same state object: only the theme changed, as when the picker previews one.
    const state = rolesState(2);
    const bar = await renderBar(state, 120);
    bar.view.applyTheme(light);
    const cells = await redraw(bar, state, 120);
    close(bar);

    expect(fgOf(cells, '✓')).toEqual([light.success]);
    expect(cellsOf(cells, 'r2')[0]?.bg).toBe(light.selectedSurface);
  });
});
