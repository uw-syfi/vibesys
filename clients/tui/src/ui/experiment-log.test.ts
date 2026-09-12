import {afterEach, describe, expect, it} from 'bun:test';
import {createTestRenderer} from '@opentui/core/testing';
import type {HypothesisEntry} from '@vibesys/backend-client';
import type {SessionController} from '../session-controller.js';
import {
  entryKey,
  initialSessionState,
  moveExperimentSelection,
  openExperimentLog,
  type SessionState,
  setExperiments,
} from '../session-model.js';
import {
  ExperimentLogView,
  entryCells,
  entryLeadingMarker,
  entryRow,
  formatMeasured,
  formatRounds,
  headerRow,
  hypothesisMetadata,
  measuredDirection,
  outcomeColor,
  outcomeLabel,
  resolveColumns,
  selectionCaret,
  sentenceCase,
  unownedRoundCells,
} from './experiment-log.js';
import {resolveTheme, THEME_NAMES} from './theme.js';

const WIDE = 120;
const NARROW = 44;

function entry(overrides: Partial<HypothesisEntry> = {}): HypothesisEntry {
  return {
    hypothesis_id: 'H-07',
    identified: true,
    claim: 'batch the prefill step',
    action: 'batch prefill',
    first_round: 41,
    last_round: 41,
    rounds: [],
    resolved_outcome: 'proven',
    judge_verdict: 'pass',
    perf_delta_pct: 12,
    kept: true,
    active: false,
    ...overrides,
  };
}

function logState(entries: HypothesisEntry[]): SessionState {
  return setExperiments(openExperimentLog(initialSessionState()), entries);
}

describe('experiment log rows', () => {
  it('renders the columns the issue asks for at a wide terminal', () => {
    const columns = resolveColumns(WIDE);
    const header = headerRow(columns);
    const row = entryRow(entry(), columns);

    for (const label of [
      'Hypothesis',
      'Rounds',
      'Implementation Details',
      'Measured',
      'Outcome',
      'Kept',
    ]) {
      expect(header).toContain(label);
    }
    expect(row).toContain('H-07');
    expect(row).toContain('41');
    expect(row).toContain('Batch the prefill step');
    expect(row).toContain('+12%');
    expect(row).toContain('Accepted');
    expect(row).not.toContain('Pass');
    expect(row.trimEnd().endsWith('Yes')).toBe(true);
  });

  it('shows hypothesis resolution without rendering the judge verdict', () => {
    const columns = resolveColumns(WIDE);

    const disproven = entryRow(
      entry({judge_verdict: 'pass', resolved_outcome: 'disproven'}),
      columns,
    );
    const rejected = entryRow(
      entry({judge_verdict: 'fail', resolved_outcome: 'rejected'}),
      columns,
    );

    expect(disproven).toContain('Rejected');
    expect(disproven).not.toContain('Pass');
    expect(rejected).toContain('Rejected');
    expect(rejected).not.toContain('Fail');
  });

  it('keeps hypothesis, rounds, and outcome when the terminal is narrow', () => {
    const columns = resolveColumns(NARROW);

    expect(columns.claim).toBe(false);
    expect(columns.kept).toBe(false);
    const row = entryRow(entry(), columns);
    expect(row).toContain('H-07');
    expect(row).toContain('41');
    expect(row).toContain('Accepted');
    expect(row).not.toContain('Batch the prefill step');
  });

  it('shows a round range for a hypothesis spanning continuations', () => {
    expect(formatRounds(entry({first_round: 42, last_round: 43}))).toBe('42-43');
    expect(formatRounds(entry({first_round: 44, last_round: 44}))).toBe('44');
  });

  it('marks the active hypothesis and leaves its outcome open', () => {
    const row = entryRow(
      entry({active: true, resolved_outcome: null, judge_verdict: null, perf_delta_pct: null}),
      resolveColumns(WIDE),
    );

    // The leading column reserves a selection caret ahead of the active
    // marker; unselected, that slot is a blank rather than absent, so the
    // active marker lands in the same place whether or not the row is
    // selected.
    expect(row.startsWith(' ▸')).toBe(true);
    expect(row).toContain('Active');
  });

  it('falls back from a delta to an absolute metric and then to a placeholder', () => {
    expect(formatMeasured(entry({perf_delta_pct: -2}))).toBe('-2.0%');
    expect(formatMeasured(entry({perf_delta_pct: null, perf_metric: 2412.5}))).toBe('2412.5');
    expect(formatMeasured(entry({perf_delta_pct: null, perf_metric: null}))).toBe('—');
  });

  it('labels an absolute metric with its unit and keeps the delta unitless', () => {
    expect(
      formatMeasured(entry({perf_delta_pct: null, perf_metric: 55434.2, perf_unit: 'ops/s'})),
    ).toBe('55434.2 ops/s');
    expect(formatMeasured(entry({perf_delta_pct: -2, perf_unit: 'ops/s'}))).toBe('-2.0%');
  });

  it('renders the three no-delta reasons and a zero delta as four distinct cells', () => {
    const noBaselineYet = formatMeasured(
      entry({
        perf_delta_pct: null,
        perf_metric: 101,
        perf_unit: 'ops/s',
        perf_delta_reason: 'no_baseline_yet',
      }),
    );
    const baselineUnresolved = formatMeasured(
      entry({
        perf_delta_pct: null,
        perf_metric: 102,
        perf_unit: 'ops/s',
        perf_delta_reason: 'baseline_unresolved',
      }),
    );
    const selfReported = formatMeasured(
      entry({perf_delta_pct: null, perf_metric: null, perf_delta_reason: 'not_framework_measured'}),
    );
    const zeroDelta = formatMeasured(entry({perf_delta_pct: 0}));

    expect(noBaselineYet).toBe('101 ops/s');
    expect(baselineUnresolved).toBe('? 102 ops/s');
    expect(selfReported).toBe('self-reported');
    expect(zeroDelta).toBe('0.0%');
    expect(new Set([noBaselineYet, baselineUnresolved, selfReported, zeroDelta]).size).toBe(4);
  });

  it('renders a legacy entry with no delta_reason as a bare value and leaves a delta unaffected', () => {
    const legacy = entry({perf_delta_pct: null, perf_metric: 2412.5, perf_unit: 'ops/s'});
    expect(legacy.perf_delta_reason).toBeUndefined();
    expect(formatMeasured(legacy)).toBe('2412.5 ops/s');

    expect(formatMeasured(entry({perf_delta_pct: 5.9}))).toBe('+5.9%');
  });

  it('points the header the way improvement goes when the log agrees on one', () => {
    const columns = resolveColumns(WIDE);

    expect(headerRow(columns, 'max')).toContain('Measured ↑');
    expect(headerRow(columns, 'min')).toContain('Measured ↓');
    expect(headerRow(columns)).not.toContain('↑');
  });

  it('finds the direction shared by every measured entry', () => {
    expect(measuredDirection([entry(), entry({perf_direction: 'max'})])).toBe('max');
    expect(measuredDirection([entry()])).toBe(null);
    expect(
      measuredDirection([entry({perf_direction: 'max'}), entry({perf_direction: 'min'})]),
    ).toBe(null);
  });

  it('spells out the measurement in the drill-down metadata', () => {
    const metadata = hypothesisMetadata(
      entry({
        perf_metric: 55434.2,
        perf_unit: 'total_ops_per_sec',
        perf_metric_name: 'total_ops_per_sec',
        perf_direction: 'max',
        perf_baseline_value: 52340.1,
        perf_delta_pct: 5.9,
      }),
    );

    expect(metadata).toContain('Metric total_ops_per_sec (maximize)');
    expect(metadata).toContain('Measured 55434.2');
    expect(metadata).toContain('Baseline 52340.1');
    expect(metadata).toContain('Delta +5.9%');
    // The unit here is the metric name; the identity clause already carries
    // it, so the numbers stay bare instead of repeating it twice.
    expect(metadata).not.toContain('55434.2 total_ops_per_sec');
  });

  it('keeps a distinct unit next to the numbers in the metadata', () => {
    const metadata = hypothesisMetadata(
      entry({
        perf_metric: 2412.5,
        perf_unit: 'ops/s',
        perf_metric_name: 'throughput',
        perf_direction: 'min',
        perf_delta_pct: null,
      }),
    );

    expect(metadata).toContain('Metric throughput (minimize)');
    expect(metadata).toContain('Measured 2412.5 ops/s');
  });

  it('spells out the baseline identity in the drill-down metadata', () => {
    const metadata = hypothesisMetadata(
      entry({
        perf_metric: 55434.2,
        perf_baseline_value: 52340.1,
        perf_baseline_round: 3,
        perf_baseline_commit: 'abc1234deadbeef',
        perf_delta_pct: 5.9,
      }),
    );

    expect(metadata).toContain('Baseline round 3');
    expect(metadata).toContain('Baseline commit abc1234');
  });

  it('spells out each no-delta reason in the drill-down metadata', () => {
    expect(
      hypothesisMetadata(entry({perf_delta_pct: null, perf_delta_reason: 'no_baseline_yet'})),
    ).toContain('No baseline existed yet');
    expect(
      hypothesisMetadata(entry({perf_delta_pct: null, perf_delta_reason: 'baseline_unresolved'})),
    ).toContain('No trusted baseline resolved');
    expect(
      hypothesisMetadata(
        entry({
          perf_delta_pct: null,
          perf_metric: null,
          perf_delta_reason: 'not_framework_measured',
        }),
      ),
    ).toContain('Self-reported, not framework-measured');
  });

  it('renders a record with no hypothesis id as an explicit placeholder', () => {
    const row = entryRow(
      entry({
        hypothesis_id: '(unidentified)',
        identified: false,
        claim: null,
        action: null,
        resolved_outcome: null,
        perf_delta_pct: null,
      }),
      resolveColumns(WIDE),
    );

    // The two-character selection-caret slot leaves one fewer column for the
    // id itself, so a 15-character placeholder now truncates one char sooner.
    expect(row).toContain('(unidentifie…');
    expect(row).toContain('—');
    expect(row).not.toContain('Active');
  });

  it('keeps an explicit gutter after a hypothesis id that fills its column', () => {
    const columns = resolveColumns(WIDE);
    const header = headerRow(columns);
    const row = entryRow(entry({hypothesis_id: 'm1-preallocated-spsc-ring'}), columns);
    const roundsStart = header.indexOf('Rounds');
    const claimStart = header.indexOf('Implementation Details');

    expect(row).toContain('m1-prealloca…');
    // Rounds right-aligns (see "right-aligns the numeric columns" below), so
    // the gutter before it is padding, not the fixed single space a
    // left-aligned column would have kept.
    expect(row[roundsStart - 1]).toBe(' ');
    // The two-digit round number lands flush against the next column's gutter
    // (the fixed two-space gap) rather than at the start of its own column.
    expect(row.slice(roundsStart, claimStart - 2).endsWith('41')).toBe(true);
  });

  it('keeps the ? marker readable when a long unit truncates at MEASURED_WIDTH', () => {
    const columns = resolveColumns(70);
    expect(columns.measured).toBe(true);
    const header = headerRow(columns);
    const measuredStart = header.indexOf('Measured');
    const row = entryRow(
      entry({
        perf_delta_pct: null,
        perf_metric: 55434.2,
        perf_unit: 'total_operations_per_second_sustained',
        perf_delta_reason: 'baseline_unresolved',
      }),
      columns,
    );

    expect(row.slice(measuredStart, measuredStart + 2)).toBe('? ');
  });

  it('keeps gutters across the separately colored outcome segments', () => {
    const cells = entryCells(entry(), resolveColumns(WIDE));

    expect(cells.outcome.startsWith('  ')).toBe(true);
    expect(cells.trailing.startsWith('  ')).toBe(true);
  });

  it('prefers the backend-supplied title over the claim and action', () => {
    const cells = entryCells(
      entry({
        title: 'Batch decode requests',
        claim: 'batch the prefill step',
        action: 'batch prefill',
      }),
      resolveColumns(WIDE),
    );

    expect(cells.leading).toContain('Batch decode requests');
    expect(cells.leading).not.toContain('batch the prefill step');
  });

  it('falls back to the claim, then the action, when there is no title', () => {
    const withClaim = entryCells(
      entry({title: null, claim: 'batch the prefill step', action: 'batch prefill'}),
      resolveColumns(WIDE),
    );
    expect(withClaim.leading).toContain('Batch the prefill step');

    const withActionOnly = entryCells(
      entry({title: null, claim: null, action: 'batch prefill'}),
      resolveColumns(WIDE),
    );
    expect(withActionOnly.leading).toContain('Batch prefill');
  });
});

/**
 * A round with agent turns but no owning hypothesis (a profiling round before
 * hypothesis 1, or any round the orchestrator has not yet attached to a
 * claim) is the default landing state, not an edge case: a task profiles
 * before proposing its first hypothesis, so nearly every run shows this row
 * first. `unownedRoundCells` has to land on the same column grid
 * `entryCells` does, or the columns above it read as unused chrome.
 */
describe('unownedRoundCells', () => {
  it('lands on the same column offsets as a hypothesis row at every width the panel degrades through', () => {
    // Real recorded round, not an invented fixture: hypothesis
    // M1-superlinear-elimination, round 1, 562.9504 total_ms, from
    // ~/dev/vibesys-runs/bad-cpp/.../agent/rounds/0001.json.
    const recorded = entry({
      hypothesis_id: 'M1-superlinear-elimination',
      first_round: 1,
      last_round: 1,
      perf_metric: 562.9504,
      perf_unit: 'total_ms',
      perf_delta_pct: null,
      resolved_outcome: 'proven',
    });
    for (const width of [120, 104, 103, 90, 89, 72, 62, 61, 54, 40]) {
      const columns = resolveColumns(width);
      const hypothesisCells = entryCells(recorded, columns);
      const roundCells = unownedRoundCells(2, columns);
      expect(roundCells.leading.length, `leading at ${width}`).toBe(hypothesisCells.leading.length);
      expect(roundCells.outcome.length, `outcome at ${width}`).toBe(hypothesisCells.outcome.length);
      expect(roundCells.trailing.length, `trailing at ${width}`).toBe(
        hypothesisCells.trailing.length,
      );
      // Same columns dropped, not just the same total width.
      expect(roundCells.trailing === '').toBe(hypothesisCells.trailing === '');
    }
  });

  it('renders absent measured, outcome, and kept values as the existing placeholder, not empty strings', () => {
    const columns = resolveColumns(WIDE);
    const cells = unownedRoundCells(3, columns);
    expect(cells.outcome.trim()).toBe('—');
    expect(cells.trailing.trim()).toBe('—');
    expect(cells.leading.trimEnd().endsWith('—')).toBe(true);
  });

  it('carries a real round number and the honest "recorded agent turns" text, never an invented claim', () => {
    const cells = unownedRoundCells(7, resolveColumns(WIDE));
    expect(cells.leading).toContain('(no hypothes');
    expect(cells.leading).toContain('7');
    expect(cells.leading).toContain('recorded agent turns');
  });

  it('keeps the selection caret in the same reserved column a hypothesis row uses', () => {
    const columns = resolveColumns(WIDE);
    const selected = unownedRoundCells(1, columns, true);
    const unselected = unownedRoundCells(1, columns, false);
    expect(selected.leading.startsWith('›')).toBe(true);
    expect(unselected.leading.startsWith(' ')).toBe(true);
    expect(selected.leading.slice(1)).toBe(unselected.leading.slice(1));
  });
});

describe('right-aligned numeric columns', () => {
  it('right-aligns Rounds so it ends flush at the column boundary regardless of digit count', () => {
    // At a width below CLAIM_MIN_WIDTH and MEASURED_MIN_WIDTH, Rounds is the
    // trailing segment of `leading`, so a right-aligned cell makes `leading`
    // end with the value itself; a left-aligned one would end with padding.
    const columns = resolveColumns(NARROW);
    expect(columns.claim).toBe(false);
    expect(columns.measured).toBe(false);
    const short = entryCells(entry({first_round: 2, last_round: 2}), columns);
    const long = entryCells(entry({first_round: 10, last_round: 99}), columns);
    expect(short.leading.endsWith('2')).toBe(true);
    expect(long.leading.endsWith('10-99')).toBe(true);
  });

  it('right-aligns Measured so it ends flush at the column boundary regardless of value length', () => {
    const columns = resolveColumns(WIDE);
    const short = entry({perf_delta_pct: null, perf_metric: 5});
    const long = entry({perf_delta_pct: null, perf_metric: 123456.78, perf_unit: 'tokens/s'});
    expect(entryCells(short, columns).leading.endsWith(formatMeasured(short))).toBe(true);
    expect(entryCells(long, columns).leading.endsWith(formatMeasured(long))).toBe(true);
  });

  it('right-aligns Kept so "Yes" and "No" both end flush at the column boundary', () => {
    const columns = resolveColumns(WIDE);
    expect(columns.kept).toBe(true);
    expect(entryCells(entry({kept: true}), columns).trailing.endsWith('Yes')).toBe(true);
    expect(entryCells(entry({kept: false}), columns).trailing.endsWith('No')).toBe(true);
  });
});

describe('selectionCaret', () => {
  it('renders a caret for the selected row and a matching blank otherwise', () => {
    expect(selectionCaret(true)).toBe('›');
    expect(selectionCaret(false)).toBe(' ');
  });
});

describe('entryLeadingMarker', () => {
  it('carries the selection caret and the active marker as independent signals', () => {
    expect(entryLeadingMarker(entry({active: false}), false)).toBe('  ');
    expect(entryLeadingMarker(entry({active: false}), true)).toBe('› ');
    expect(entryLeadingMarker(entry({active: true}), false)).toBe(' ▸');
    expect(entryLeadingMarker(entry({active: true}), true)).toBe('›▸');
  });
});

describe('entryCells and entryRow with selection', () => {
  it('shows the caret only on the selected row, at the same column as an unselected row', () => {
    const columns = resolveColumns(WIDE);
    const selected = entryRow(entry({hypothesis_id: 'H-01'}), columns, true);
    const unselected = entryRow(entry({hypothesis_id: 'H-01'}), columns, false);

    expect(selected.startsWith('›')).toBe(true);
    expect(unselected.startsWith(' ')).toBe(true);
    // Everything past the reserved caret column is identical: selection never
    // reflows the row's other columns.
    expect(selected.slice(1)).toBe(unselected.slice(1));
  });

  it('defaults to unselected when the caller does not pass a selection flag', () => {
    const columns = resolveColumns(WIDE);
    expect(entryRow(entry(), columns)).toBe(entryRow(entry(), columns, false));
    expect(entryCells(entry(), columns)).toEqual(entryCells(entry(), columns, false));
  });
});

describe('experiment log layout', () => {
  it('fits the panel exactly at every width it degrades through', () => {
    for (const width of [120, 104, 103, 90, 89, 72, 62, 61, 54, 40]) {
      const columns = resolveColumns(width);
      const header = headerRow(columns);
      const row = entryRow(entry(), columns);
      expect(header.length, `header at ${width}`).toBeLessThanOrEqual(width);
      expect(row.length, `row at ${width}`).toBeLessThanOrEqual(width);
    }
  });
});

describe('experiment log outcome color', () => {
  it('reads green for a hypothesis that held and red for one that did not', () => {
    const theme = resolveTheme('dark');

    expect(outcomeColor(theme, entry({resolved_outcome: 'proven'}))).toBe(theme.success);
    expect(outcomeColor(theme, entry({resolved_outcome: 'disproven'}))).toBe(theme.error);
    expect(outcomeColor(theme, entry({resolved_outcome: 'rejected'}))).toBe(theme.error);
  });

  it('leaves outcomes with no verdict reading in body text', () => {
    const theme = resolveTheme('dark');

    expect(outcomeColor(theme, entry({resolved_outcome: 'continue'}))).toBe(theme.textPrimary);
    expect(outcomeColor(theme, entry({resolved_outcome: 'inconclusive'}))).toBe(theme.textPrimary);
    expect(outcomeColor(theme, entry({resolved_outcome: null}))).toBe(theme.textPrimary);
  });

  it('uses the active accent while a hypothesis is still open', () => {
    const theme = resolveTheme('dark');
    const open = entry({active: true, resolved_outcome: null});

    expect(outcomeColor(theme, open)).toBe(theme.warning);
  });

  it('takes every color from the selected theme, never a literal', () => {
    for (const name of THEME_NAMES) {
      const theme = resolveTheme(name);
      expect(outcomeColor(theme, entry({resolved_outcome: 'proven'}))).toBe(theme.success);
      expect(outcomeColor(theme, entry({resolved_outcome: 'disproven'}))).toBe(theme.error);
    }
  });

  it('maps backend resolutions to operator-facing acceptance labels', () => {
    const columns = resolveColumns(WIDE);

    expect(entryRow(entry({resolved_outcome: 'proven'}), columns)).toContain('Accepted');
    expect(entryRow(entry({resolved_outcome: 'disproven'}), columns)).toContain('Rejected');
    expect(outcomeLabel(entry({resolved_outcome: 'rejected'}))).toBe('Rejected');
    expect(outcomeLabel(entry({resolved_outcome: 'inconclusive'}))).toBe('Inconclusive');
  });
});

describe('sentenceCase', () => {
  it('capitalises the first letter and leaves the rest alone', () => {
    expect(sentenceCase('batch the prefill step')).toBe('Batch the prefill step');
    expect(sentenceCase('implementation_failed')).toBe('Implementation_failed');
    expect(sentenceCase('KV cache block')).toBe('KV cache block');
    expect(sentenceCase('—')).toBe('—');
    expect(sentenceCase('')).toBe('');
  });
});

describe('experiment log selection', () => {
  it('keys placeholder rows by round so duplicates stay distinct', () => {
    const rows = [
      entry({hypothesis_id: '(unidentified)', identified: false, first_round: 1, last_round: 1}),
      entry({hypothesis_id: '(unidentified)', identified: false, first_round: 2, last_round: 2}),
    ];

    expect(rows.map(entryKey)).toEqual(['(unidentified)#1', '(unidentified)#2']);
  });

  it('starts on the active hypothesis and clamps at both ends', () => {
    let state = logState([
      entry({hypothesis_id: 'H-01', first_round: 1, last_round: 1}),
      entry({hypothesis_id: 'H-02', first_round: 2, last_round: 2, active: true}),
    ]);
    expect(state.experimentLog?.selectedId).toBe('H-02');

    state = moveExperimentSelection(state, 5);
    expect(state.experimentLog?.selectedId).toBe('H-02');

    state = moveExperimentSelection(state, -5);
    expect(state.experimentLog?.selectedId).toBe('H-01');
  });

  it('drops a selection whose hypothesis disappears from the log', () => {
    const first = logState([entry({hypothesis_id: 'H-01', first_round: 1, last_round: 1})]);
    const replaced = setExperiments(first, [
      entry({hypothesis_id: 'H-99', first_round: 9, last_round: 9}),
    ]);

    expect(replaced.experimentLog?.selectedId).toBe('H-99');
  });
});

/**
 * The pure helpers above prove the caret occupies a reserved column and that
 * an unowned round's cells share a hypothesis row's widths; these tests
 * reproduce both the original selection symptom and the column-alignment
 * defect through the real OpenTUI test renderer, per
 * coding-best-practices.md's rule that a terminal-geometry symptom needs the
 * renderer, not just a formatter test.
 */
describe('experiment log rendered rows', () => {
  const cleanup: Array<() => void> = [];

  afterEach(() => {
    for (const destroy of cleanup.splice(0).reverse()) destroy();
  });

  /** None of these fire in a render-only test; onMouseUp is never simulated. */
  const controller = {
    focusPane: () => {},
    openHypothesisDetail: () => {},
    moveExperimentSelection: () => {},
    selectExperimentActivity: () => {},
    openRound: () => {},
  } as unknown as SessionController;

  async function renderLog(state: SessionState): Promise<string> {
    const testRenderer = await createTestRenderer({width: 100, height: 24});
    const view = new ExperimentLogView(testRenderer.renderer, controller, resolveTheme(null));
    testRenderer.renderer.root.add(view.output);
    cleanup.push(() => {
      view.destroy();
      view.output.destroyRecursively();
      testRenderer.renderer.destroy();
    });
    view.render(state);
    await testRenderer.renderOnce();
    return testRenderer.captureCharFrame();
  }

  it('puts the caret at the same column on the selected row as the blank it replaces elsewhere', async () => {
    const initial = logState([
      entry({hypothesis_id: 'H-01', first_round: 1, last_round: 1}),
      entry({hypothesis_id: 'H-02', first_round: 2, last_round: 2}),
    ]);
    expect(initial.experimentLog?.selectedId).toBe('H-01');

    const h01Selected = (await renderLog(initial)).split('\n');
    const rowH01Selected = h01Selected.findIndex(line => line.includes('H-01'));
    const rowH02Unselected = h01Selected.findIndex(line => line.includes('H-02'));
    const colH01 = h01Selected[rowH01Selected]?.indexOf('H-01') ?? -1;
    const colH02 = h01Selected[rowH02Unselected]?.indexOf('H-02') ?? -1;
    expect(colH01).toBeGreaterThan(0);
    expect(colH01).toBe(colH02);
    // The active marker sits directly before the id; the caret is one column
    // further left again, so it never displaces the marker or the id.
    expect(h01Selected[rowH01Selected]?.[colH01 - 2]).toBe('›');
    expect(h01Selected[rowH02Unselected]?.[colH02 - 2]).toBe(' ');

    const moved = moveExperimentSelection(initial, 1);
    expect(moved.experimentLog?.selectedId).toBe('H-02');
    const h02Selected = (await renderLog(moved)).split('\n');
    const rowH01AfterMove = h02Selected.findIndex(line => line.includes('H-01'));
    const rowH02AfterMove = h02Selected.findIndex(line => line.includes('H-02'));

    // Moving the selection off H-01 does not reflow its row: the id lands in
    // exactly the column it held while selected.
    expect(h02Selected[rowH01AfterMove]?.indexOf('H-01')).toBe(colH01);
    expect(h02Selected[rowH02AfterMove]?.indexOf('H-02')).toBe(colH02);
    expect(h02Selected[rowH01AfterMove]?.[colH01 - 2]).toBe(' ');
    expect(h02Selected[rowH02AfterMove]?.[colH02 - 2]).toBe('›');
  });

  it('marks the selected unowned round row with the same caret, in its own reserved column', async () => {
    const withActivity = setExperiments(
      openExperimentLog({
        ...initialSessionState(),
        core: {
          ...initialSessionState().core,
          rounds: [
            {number: 3, status: 'completed'},
            {number: 4, status: 'completed'},
          ],
        },
      }),
      [],
    );
    const frame = (await renderLog(withActivity)).split('\n');
    // "recorded agent turns" is the round row's Implementation Details cell;
    // both round rows carry it, in ascending round order (3, then 4), and
    // round 3 is selected by default since it is the first unowned round.
    const roundLines = frame.filter(line => line.includes('recorded agent turns'));
    expect(roundLines).toHaveLength(2);
    const [selectedLine, unselectedLine] = roundLines as [string, string];
    const colSelected = selectedLine.indexOf('(no hypothes');
    const colUnselected = unselectedLine.indexOf('(no hypothes');
    expect(colSelected).toBeGreaterThan(0);
    expect(colSelected).toBe(colUnselected);
    // The caret sits two columns before the identity text: one column for
    // itself, one for the space that always follows it, exactly like a
    // hypothesis row's marker.
    expect(selectedLine[colSelected - 2]).toBe('›');
    expect(unselectedLine[colUnselected - 2]).toBe(' ');
  });

  it('aligns an unowned round row to the same column offsets as a hypothesis row sharing the same frame', async () => {
    const state = setExperiments(
      openExperimentLog({
        ...initialSessionState(),
        core: {...initialSessionState().core, rounds: [{number: 5, status: 'completed'}]},
      }),
      [entry({hypothesis_id: 'H-01', first_round: 1, last_round: 1})],
    );
    const frame = (await renderLog(state)).split('\n');
    const hypothesisLine = frame.find(line => line.includes('H-01'));
    const roundLine = frame.find(line => line.includes('recorded agent turns'));
    if (hypothesisLine === undefined || roundLine === undefined) {
      throw new Error('expected both a hypothesis row and an unowned round row');
    }
    // Both rows reserve the same two-character marker slot before their
    // identity text: this is the defect the issue reported, restated as a
    // column offset rather than as a screenshot.
    expect(roundLine.indexOf('(no hypothes')).toBe(hypothesisLine.indexOf('H-01'));
  });

  it('draws a rule under the header that reserves its row regardless of how many rows follow', async () => {
    const oneRound = setExperiments(
      openExperimentLog({
        ...initialSessionState(),
        core: {...initialSessionState().core, rounds: [{number: 1, status: 'completed'}]},
      }),
      [],
    );
    const twoRounds = setExperiments(
      openExperimentLog({
        ...initialSessionState(),
        core: {
          ...initialSessionState().core,
          rounds: [
            {number: 1, status: 'completed'},
            {number: 2, status: 'completed'},
          ],
        },
      }),
      [],
    );
    const frameOne = (await renderLog(oneRound)).split('\n');
    const frameTwo = (await renderLog(twoRounds)).split('\n');
    const headerIndexOne = frameOne.findIndex(
      line => line.includes('Hypothesis') && line.includes('Rounds'),
    );
    const headerIndexTwo = frameTwo.findIndex(
      line => line.includes('Hypothesis') && line.includes('Rounds'),
    );
    expect(headerIndexOne).toBeGreaterThanOrEqual(0);
    // The rule sits at a fixed offset from the header whether one row follows
    // it or two: nothing above the rows moves when a row appears.
    expect(headerIndexOne).toBe(headerIndexTwo);
    const ruleOne = frameOne[headerIndexOne + 1] ?? '';
    const ruleTwo = frameTwo[headerIndexTwo + 1] ?? '';
    // The pane's own border/padding sit either side of the line; the rule
    // itself is the contiguous run of the rule glyph in the middle.
    const ruleRun = ruleOne.match(/─+/)?.[0] ?? '';
    expect(ruleRun.length).toBeGreaterThan(40);
    expect(ruleOne).toBe(ruleTwo);
  });

  it('gives the unowned-round state a next-step line distinct from the zero-row wording', async () => {
    const zeroRows = logState([]);
    const zeroFrame = (await renderLog(zeroRows)).split('\n');
    // Unchanged: the existing zero-row empty state keeps its own wording.
    expect(zeroFrame.some(line => line.includes('No hypotheses have been recorded yet.'))).toBe(
      true,
    );
    expect(
      zeroFrame.some(line =>
        line.includes('The first one appears once the orchestrator has planned a round.'),
      ),
    ).toBe(true);

    const unownedRound = setExperiments(
      openExperimentLog({
        ...initialSessionState(),
        core: {...initialSessionState().core, rounds: [{number: 1, status: 'completed'}]},
      }),
      [],
    );
    const roundFrame = (await renderLog(unownedRound)).split('\n');
    // A round has genuinely run here, unlike the zero-row case, so the
    // wording does not repeat "No hypotheses have been recorded yet."
    expect(roundFrame.some(line => line.includes('No hypotheses have been recorded yet.'))).toBe(
      false,
    );
    expect(
      roundFrame.some(line => line.includes('The first hypothesis appears once the orchestrator')),
    ).toBe(true);
  });
});
