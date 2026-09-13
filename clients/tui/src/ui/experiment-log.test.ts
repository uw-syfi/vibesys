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
} from './experiment-log.js';
import {displayWidth} from './text-width.js';
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

    expect(row).toContain('m1-prealloca…  41');
    expect(row[roundsStart - 1]).toBe(' ');
    expect(row.slice(roundsStart).startsWith('41')).toBe(true);
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
 * Column arithmetic in cells, not code units: a CJK character is one code
 * unit but two terminal cells, so `padEnd`/`slice` layouts drift as soon as a
 * title or id carries one.
 */
describe('experiment log CJK column alignment', () => {
  function columnOf(row: string, needle: string): number {
    const index = row.indexOf(needle);
    expect(index, `${needle} in ${row}`).toBeGreaterThanOrEqual(0);
    return displayWidth(row.slice(0, index));
  }

  it('keeps the outcome column aligned when the claim is CJK', () => {
    const columns = resolveColumns(WIDE);
    const ascii = entryRow(entry(), columns);
    const cjk = entryRow(entry({title: '缓存优化提升吞吐'}), columns);

    expect(displayWidth(cjk)).toBe(displayWidth(ascii));
    expect(columnOf(cjk, 'Accepted')).toBe(columnOf(ascii, 'Accepted'));
  });

  it('truncates a CJK claim by cells and never splits a wide character', () => {
    const columns = resolveColumns(WIDE);
    const cells = entryCells(entry({title: '性'.repeat(60)}), columns);

    // The claim budget is odd here, so the last ideograph would straddle it:
    // it is dropped whole and the ellipsis marks the cut.
    expect(cells.leading).toContain(`${'性'.repeat(25)}…`);
    expect(cells.leading).not.toContain('性'.repeat(26));
    expect(displayWidth(cells.leading)).toBe(displayWidth(entryCells(entry(), columns).leading));
  });

  it('truncates a CJK hypothesis id by cells so the rounds column stays put', () => {
    const columns = resolveColumns(WIDE);
    const ascii = entryRow(entry(), columns);
    const cjk = entryRow(entry({hypothesis_id: '缓存优化假设编号很长'}), columns);

    expect(cjk).toContain('缓存优化假设…');
    expect(columnOf(cjk, '41')).toBe(columnOf(ascii, '41'));
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
 * The pure helpers above prove the caret occupies a reserved column; these
 * tests reproduce the symptom the issue reported (selection legible only by a
 * background swap) through the real OpenTUI test renderer, per
 * coding-best-practices.md's rule that a terminal-geometry symptom needs the
 * renderer, not just a formatter test.
 */
describe('experiment log rendered selection glyph', () => {
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

  it('keeps the outcome column aligned on screen when a claim is CJK', async () => {
    const frame = (
      await renderLog(
        logState([
          entry({hypothesis_id: 'H-01', first_round: 1, last_round: 1}),
          entry({hypothesis_id: 'H-02', first_round: 2, last_round: 2, title: '缓存优化提升吞吐'}),
        ]),
      )
    ).split('\n');
    const asciiRow = frame.find(line => line.includes('H-01'));
    const cjkRow = frame.find(line => line.includes('H-02'));
    expect(asciiRow).toBeDefined();
    expect(cjkRow).toBeDefined();
    if (asciiRow === undefined || cjkRow === undefined) throw new Error('rows did not render');

    // The frame stores a wide character once per grapheme, so the on-screen
    // column of a cell is the display width of everything before it, not its
    // string index.
    const columnOf = (line: string, needle: string): number => {
      const index = line.indexOf(needle);
      expect(index, `${needle} in ${line}`).toBeGreaterThanOrEqual(0);
      return displayWidth(line.slice(0, index));
    };
    expect(columnOf(cjkRow, 'Accepted')).toBe(columnOf(asciiRow, 'Accepted'));
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
    const rowSelected = frame.findIndex(line => line.includes('Round 3'));
    const rowUnselected = frame.findIndex(line => line.includes('Round 4'));
    const colSelected = frame[rowSelected]?.indexOf('Round 3') ?? -1;
    const colUnselected = frame[rowUnselected]?.indexOf('Round 4') ?? -1;
    expect(colSelected).toBeGreaterThan(0);
    expect(colSelected).toBe(colUnselected);
    // The caret sits two columns before "Round": one column for itself, one
    // for the space that always follows it.
    expect(frame[rowSelected]?.[colSelected - 2]).toBe('›');
    expect(frame[rowUnselected]?.[colUnselected - 2]).toBe(' ');
  });
});
