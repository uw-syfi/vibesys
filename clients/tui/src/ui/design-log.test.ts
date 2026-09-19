import {describe, expect, it} from 'bun:test';
import {create, type MessageInitShape} from '@bufbuild/protobuf';
import {
  CandidateDisposition,
  DesignChange,
  type DesignFileChange,
  DesignFileChangeSchema,
  type HypothesisRound,
  HypothesisOutcome,
  HypothesisRoundSchema,
  RoundReviewVerdict,
} from '@vibesys/backend-client';
import type {DesignRoundView} from '../session-model.js';
import {
  designRoundHeading,
  designStageSummary,
  fileChangeCounts,
  fileChangeGlyph,
  formatFileChange,
  renderDesignSummary,
} from './design-log.js';

function record(overrides: MessageInitShape<typeof HypothesisRoundSchema> = {}): HypothesisRound {
  return create(HypothesisRoundSchema, {round: 1, passed: false, reviewed: false, ...overrides});
}

function fileChange(path: string, change: DesignChange, renamedFrom?: string): DesignFileChange {
  return create(DesignFileChangeSchema, {
    path,
    change,
    ...(renamedFrom === undefined ? {} : {renamedFrom}),
  });
}

function view(overrides: Partial<DesignRoundView> = {}): DesignRoundView {
  return {round: 1, files: null, hypothesisId: null, title: null, record: null, ...overrides};
}

describe('file change formatting', () => {
  it('gives each change kind its own glyph', () => {
    expect(fileChangeGlyph(DesignChange.ADDED)).toBe('+');
    expect(fileChangeGlyph(DesignChange.DELETED)).toBe('-');
    expect(fileChangeGlyph(DesignChange.RENAMED)).toBe('→');
    expect(fileChangeGlyph(DesignChange.MODIFIED)).toBe('~');
  });

  it('names the old path of a rename and only that', () => {
    expect(
      formatFileChange(fileChange('src/lib.rs', DesignChange.RENAMED, 'src/queue.rs')),
    ).toBe('→ src/lib.rs (was src/queue.rs)');
    expect(formatFileChange(fileChange('src/ring.rs', DesignChange.ADDED))).toBe('+ src/ring.rs');
    expect(formatFileChange(fileChange('src/ffi.rs', DesignChange.DELETED))).toBe('- src/ffi.rs');
  });

  it('tallies per kind in a fixed order and drops empty kinds', () => {
    expect(
      fileChangeCounts([
        fileChange('a', DesignChange.MODIFIED),
        fileChange('b', DesignChange.ADDED),
        fileChange('c', DesignChange.ADDED),
        fileChange('d', DesignChange.RENAMED, 'e'),
      ]),
    ).toBe('+2 ~1 →1');
    expect(fileChangeCounts([])).toBeNull();
  });
});

describe('designStageSummary', () => {
  it('reads every stage fact from the experiment log record', () => {
    expect(
      designStageSummary(
        view({
          record: record({
            hypothesisOutcome: HypothesisOutcome.PROVEN,
            judgeVerdict: RoundReviewVerdict.PASS,
            officialEvaluation: true,
            candidateDisposition: CandidateDisposition.PARETO_FRONTIER,
            commit: '0123456789abcdef0123456789abcdef01234567',
          }),
        }),
      ),
    ).toBe(
      'Outcome proven · Judge pass · Official evaluation · Candidate pareto_frontier · Checkpoint 0123456789',
    );
  });

  it('is absent for a round the experiment log has no row for', () => {
    expect(designStageSummary(view())).toBeNull();
  });

  it('is absent for a round with no recorded stages', () => {
    expect(designStageSummary(view({record: record()}))).toBeNull();
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
        record: record({perfMetric: 2400, perfUnit: 'ops/s', perfDeltaPct: 12.5}),
        files: [
          fileChange('src/ring.rs', DesignChange.ADDED),
          fileChange('src/lib.rs', DesignChange.MODIFIED),
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
    const files = Array.from({length: 6}, (_, index) =>
      fileChange(`src/file-${index}.rs`, DesignChange.MODIFIED),
    );
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
