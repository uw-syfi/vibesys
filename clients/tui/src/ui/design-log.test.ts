import {describe, expect, it} from 'bun:test';
import type {HypothesisRound} from '@vibesys/backend-client';
import {
  designRoundHeading,
  fileChangeCounts,
  fileChangeGlyph,
  formatFileChange,
  renderDesignSummary,
} from '../design-log.js';
import type {DesignRoundView} from '../experiments.js';

function record(overrides: Partial<HypothesisRound> = {}): HypothesisRound {
  return {round: 1, passed: false, reviewed: false, ...overrides};
}

function view(overrides: Partial<DesignRoundView> = {}): DesignRoundView {
  return {round: 1, files: null, hypothesisId: null, title: null, record: null, ...overrides};
}

describe('file change formatting', () => {
  it('gives each change kind its own glyph', () => {
    expect(fileChangeGlyph('added')).toBe('+');
    expect(fileChangeGlyph('deleted')).toBe('-');
    expect(fileChangeGlyph('renamed')).toBe('→');
    expect(fileChangeGlyph('modified')).toBe('~');
  });

  it('names the old path of a rename and only that', () => {
    expect(
      formatFileChange({path: 'src/lib.rs', change: 'renamed', renamed_from: 'src/queue.rs'}),
    ).toBe('→ src/lib.rs (was src/queue.rs)');
    expect(formatFileChange({path: 'src/ring.rs', change: 'added'})).toBe('+ src/ring.rs');
    expect(formatFileChange({path: 'src/ffi.rs', change: 'deleted'})).toBe('- src/ffi.rs');
  });

  it('tallies per kind in a fixed order and drops empty kinds', () => {
    expect(
      fileChangeCounts([
        {path: 'a', change: 'modified'},
        {path: 'b', change: 'added'},
        {path: 'c', change: 'added'},
        {path: 'd', change: 'renamed', renamed_from: 'e'},
      ]),
    ).toBe('+2 ~1 →1');
    expect(fileChangeCounts([])).toBeNull();
  });
});

describe('designRoundHeading', () => {
  it('names the owning hypothesis when the join found one', () => {
    expect(designRoundHeading(view({hypothesisId: 'H-01', title: 'Pad the indices'}))).toBe(
      'Round 1 · H-01 · Pad the indices',
    );
    expect(designRoundHeading(view({hypothesisId: 'H-01'}))).toBe('Round 1 · H-01');
    expect(designRoundHeading(view())).toBe('Round 1');
  });
});

describe('renderDesignSummary', () => {
  it('explains an empty log instead of rendering nothing', () => {
    expect(renderDesignSummary([])).toContain('No rounds have been recorded yet.');
  });

  it('renders a heading and a file line per round', () => {
    const rendered = renderDesignSummary([
      view({
        hypothesisId: 'H-01',
        title: 'Pad the indices',
        record: record({perf_metric: 2400, perf_unit: 'ops/s', perf_delta_pct: 12.5}),
        files: [
          {path: 'src/ring.rs', change: 'added'},
          {path: 'src/lib.rs', change: 'modified'},
        ],
      }),
    ]);
    expect(rendered).toContain('Round 1 · H-01 · Pad the indices · 2400 ops/s (+13%)');
    expect(rendered).toContain('  +1 ~1  src/ring.rs, src/lib.rs');
  });

  it('distinguishes unrecorded changes from a round that changed nothing', () => {
    const rendered = renderDesignSummary([view({round: 1}), view({round: 2, files: []})]);
    expect(rendered).toContain('file changes not recorded');
    expect(rendered).toContain('no workspace files changed');
  });

  it('elides file names past the inline limit into a count', () => {
    const files = Array.from({length: 6}, (_, index) => ({
      path: `src/file-${index}.rs`,
      change: 'modified' as const,
    }));
    const rendered = renderDesignSummary([view({files})]);
    expect(rendered).toContain('src/file-3.rs, +2 more');
    expect(rendered).not.toContain('src/file-4.rs');
  });

  it('keeps every round, because the pane scrolls', () => {
    const rounds = Array.from({length: 12}, (_, index) => view({round: index + 1}));
    const rendered = renderDesignSummary(rounds);
    for (let number = 1; number <= 12; number += 1) {
      expect(rendered).toContain(`Round ${number}`);
    }
  });
});
