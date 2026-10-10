import {describe, expect, it} from 'bun:test';
import type {HypothesisEntry, ProtocolResponse} from '@vibesys/backend-client';
import {setDesignLog, setExperiments} from '../experiments.js';
import {PLOT_WIDTH} from '../performance-chart.js';
import {
  initialSessionState,
  openPane,
  setDesignPane,
  setPerformancePane,
} from '../session-model.js';
import {MIN_SPLIT_WIDTH, rightPaneContent, rightPaneWidth, splitFits} from './right-pane.js';

/** Border (1) and padding (1) columns on each side of the pane. */
const PANE_BORDER_AND_PADDING = 4;

describe('split thresholds', () => {
  it('splits only once both panes would be readable', () => {
    expect(splitFits(MIN_SPLIT_WIDTH)).toBe(true);
    expect(splitFits(MIN_SPLIT_WIDTH - 1)).toBe(false);
    expect(splitFits(80)).toBe(false);
    expect(splitFits(200)).toBe(true);
  });
});

describe('pane sizing', () => {
  it('scales with the terminal instead of a fixed percentage', () => {
    // The old overlay was 70% at every size; these differ from each other.
    expect(rightPaneWidth(100)).not.toBe(rightPaneWidth(160));
    expect(rightPaneWidth(160)).toBeGreaterThan(rightPaneWidth(120));
  });

  it('always leaves the transcript a readable column', () => {
    for (let width = MIN_SPLIT_WIDTH; width <= 300; width += 1) {
      const left = width - rightPaneWidth(width);
      expect(left, `left pane at ${width}`).toBeGreaterThanOrEqual(38);
    }
  });

  it('keeps the chart within the pane at the narrowest split', () => {
    // The performance chart is 48 plot columns plus an 8-column axis gutter;
    // below that the visualization is the thing that breaks.
    expect(rightPaneWidth(MIN_SPLIT_WIDTH)).toBeGreaterThanOrEqual(56);
  });

  it('gives the chart its full structural width at the narrowest split, derived from PLOT_WIDTH', () => {
    // The pane's content area (its width minus border and padding) must fit
    // the chart's widest structural row: the 10-column axis/label gutter
    // plus PLOT_WIDTH plot columns. This ties the two together through
    // PLOT_WIDTH itself, so they cannot drift apart the way the old
    // hand-copied RIGHT_PANE_MIN literal could.
    const contentWidth = rightPaneWidth(MIN_SPLIT_WIDTH) - PANE_BORDER_AND_PADDING;
    expect(contentWidth).toBeGreaterThanOrEqual(PLOT_WIDTH + 10);
  });

  it('stops widening the pane on very wide terminals', () => {
    expect(rightPaneWidth(400)).toBe(rightPaneWidth(300));
  });
});

describe('structured pane sources', () => {
  it('renders a design refresh from the authoritative session design log', () => {
    let state = setDesignPane(
      openPane(initialSessionState(), 'design'),
      [{round: 1, files: [{path: 'src/old.rs', change: 'modified'}]}],
      true,
    );
    const pane = state.layout.right;
    if (pane?.view !== 'design') throw new Error('expected design pane');
    expect(rightPaneContent(state, pane)).toContain('src/old.rs');

    state = setDesignLog(state, [{round: 2, files: [{path: 'src/new.rs', change: 'added'}]}]);

    expect(rightPaneContent(state, pane)).toContain('src/new.rs');
    expect(rightPaneContent(state, pane)).not.toContain('src/old.rs');
  });

  it('reads late experiment direction without replacing the performance query data', () => {
    let state = setPerformancePane(
      openPane(initialSessionState(), 'perf'),
      {
        performance: [performance(1, 10), performance(2, 20)],
        events: [],
      },
      performanceContext(null),
    );
    const pane = state.layout.right;
    if (pane?.view !== 'perf') throw new Error('expected performance pane');
    expect(rightPaneContent(state, pane)).toContain('best r2 20 ops/s');

    state = setExperiments(state, [experiment('min')]);

    expect(rightPaneContent(state, pane)).toContain('minimize ↓');
    expect(rightPaneContent(state, pane)).toContain('best r1 10 ops/s');
  });

  it('keeps the performance query direction authoritative over stale experiments', () => {
    let state = setPerformancePane(
      openPane(initialSessionState(), 'perf'),
      {
        performance: [performance(1, 10), performance(2, 20)],
        events: [],
      },
      performanceContext('max'),
    );
    const pane = state.layout.right;
    if (pane?.view !== 'perf') throw new Error('expected performance pane');

    state = setExperiments(state, [experiment('min')]);

    expect(rightPaneContent(state, pane)).toContain('maximize ↑');
    expect(rightPaneContent(state, pane)).toContain('best r2 20 ops/s');
  });
});

function performance(
  round: number,
  value: number,
): NonNullable<ProtocolResponse['performance']>[number] {
  return {round, perf_metric: value, perf_unit: 'ops/s', passed: true};
}

function performanceContext(
  direction: Exclude<
    NonNullable<ProtocolResponse['performance_context']>['objective_direction'],
    undefined
  >,
): NonNullable<ProtocolResponse['performance_context']> {
  return {
    objective_metric: 'ops/s',
    objective_direction: direction,
    objective_baseline_value: null,
    objective_baseline_round: null,
    objective_baseline_commit: null,
    objective_unit: null,
    objective_description: null,
  };
}

function experiment(direction: NonNullable<HypothesisEntry['perf_direction']>): HypothesisEntry {
  return {
    hypothesis_id: 'H-01',
    first_round: 1,
    last_round: 2,
    perf_direction: direction,
  };
}
