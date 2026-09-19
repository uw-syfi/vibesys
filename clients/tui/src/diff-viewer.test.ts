import {describe, expect, it} from 'bun:test';
import {create} from '@bufbuild/protobuf';
import {
  DesignChange,
  type DesignFileChange,
  DesignFileChangeSchema,
  type DesignPatch,
  DesignPatchSchema,
  type DesignRound,
  DesignRoundSchema,
} from '@vibesys/backend-client';
import {
  applyDiffPatch,
  closeDiffViewer,
  diffExternalCommand,
  diffHunkDisplayLine,
  diffHunkLines,
  diffRangeExplanation,
  diffRoundRange,
  diffViewerLines,
  diffViewerTitle,
  failDiffPatch,
  markDiffPatchLoading,
  moveDiffFile,
  moveDiffHunk,
  openDiffViewer,
} from './diff-viewer.js';
import {
  closeOverlays,
  type DiffViewerState,
  initialSessionState,
  type SessionState,
  showDetail,
  showLive,
} from './session-model.js';

const FILES: DesignFileChange[] = [
  create(DesignFileChangeSchema, {path: 'src/lib.rs', change: DesignChange.MODIFIED}),
  create(DesignFileChangeSchema, {path: 'src/new.rs', change: DesignChange.ADDED}),
  create(DesignFileChangeSchema, {
    path: 'src/moved.rs',
    change: DesignChange.RENAMED,
    renamedFrom: 'src/old.rs',
  }),
];

interface RoundOverrides {
  base?: string | undefined;
  commit?: string | undefined;
  files?: {changes: DesignFileChange[]} | undefined;
}

function designRound(overrides: RoundOverrides = {}): DesignRound {
  return create(DesignRoundSchema, {
    round: 3,
    base: 'aaa1111',
    commit: 'bbb2222',
    files: {changes: FILES},
    ...overrides,
  });
}

function designPatch(init: {
  path?: string;
  base?: string;
  patch?: string;
  truncated?: boolean;
}): DesignPatch {
  return create(DesignPatchSchema, {base: 'aaa1111', head: 'bbb2222', path: 'src/lib.rs', ...init});
}

/** A two-hunk patch shaped like real `git diff` output, trailing newline included. */
const PATCH = [
  'diff --git a/src/lib.rs b/src/lib.rs',
  'index 1111111..2222222 100644',
  '--- a/src/lib.rs',
  '+++ b/src/lib.rs',
  '@@ -1,3 +1,4 @@',
  ' fn main() {',
  '+    init();',
  ' }',
  '@@ -10,2 +11,2 @@',
  '-let width = 1;',
  '+let width = 2;',
  '',
].join('\n');

function openedState(): SessionState {
  const range = diffRoundRange(designRound());
  if (range === null) throw new Error('fixture round must be diffable');
  return openDiffViewer(initialSessionState(), range);
}

function viewerOf(state: SessionState): DiffViewerState {
  const viewer = state.diffViewer;
  if (viewer === null) throw new Error('expected an open diff viewer');
  return viewer;
}

/** The loaded-patch state most render tests start from. */
function loadedState(patch: string = PATCH, truncated = false): SessionState {
  const state = markDiffPatchLoading(openedState(), 'src/lib.rs');
  return applyDiffPatch(state, designPatch({patch, truncated}));
}

describe('diffRoundRange', () => {
  it('carries the round range the design log recorded', () => {
    expect(diffRoundRange(designRound())).toEqual({
      round: 3,
      base: 'aaa1111',
      head: 'bbb2222',
      files: FILES,
    });
  });

  it('declines rounds missing any leg of the range, each with its own wording', () => {
    // Cheapest first: the round ran but touched nothing.
    const empty = designRound({files: {changes: []}});
    expect(diffRoundRange(empty)).toBeNull();
    expect(diffRangeExplanation(empty)).toBe('Round 3 changed no workspace files.');

    // The server could not read the repository: the null file list is the
    // same degradation the change list itself shows.
    const unread = designRound({files: undefined});
    expect(diffRoundRange(unread)).toBeNull();
    expect(diffRangeExplanation(unread)).toContain('file changes are not recorded');

    for (const partial of [designRound({base: undefined}), designRound({commit: undefined})]) {
      expect(diffRoundRange(partial)).toBeNull();
      expect(diffRangeExplanation(partial)).toBe('Round 3 has no commit range to diff.');
    }
  });
});

describe('opening and navigation', () => {
  it('opens on the first file with no patches fetched, replacing any overlay', () => {
    const range = diffRoundRange(designRound());
    if (range === null) throw new Error('fixture round must be diffable');
    const behind = showDetail(initialSessionState(), 'Round 3');
    const state = openDiffViewer(behind, range);
    expect(state.overlay).toBeNull();
    expect(viewerOf(state)).toMatchObject({round: 3, index: 0, hunk: 0, patches: {}});
  });

  it('moves file to file, clamping at both ends and restarting at the first hunk', () => {
    const opened = loadedState();
    expect(moveDiffFile(opened, -1)).toBe(opened);

    const onHunk = moveDiffHunk(opened, 1);
    const next = moveDiffFile(onHunk, 1);
    expect(viewerOf(next).index).toBe(1);
    expect(viewerOf(next).hunk).toBe(0);
    // The cache survives the move: coming back does not refetch.
    expect(viewerOf(next).patches['src/lib.rs']).toEqual(viewerOf(onHunk).patches['src/lib.rs']);

    const last = moveDiffFile(next, 5);
    expect(viewerOf(last).index).toBe(FILES.length - 1);
    expect(moveDiffFile(last, 1)).toBe(last);
  });

  it('moves hunk to hunk within the loaded patch, holding at either end', () => {
    const opened = loadedState();
    expect(viewerOf(moveDiffHunk(opened, 1)).hunk).toBe(1);
    expect(viewerOf(moveDiffHunk(moveDiffHunk(opened, 1), 1)).hunk).toBe(1);
    expect(moveDiffHunk(opened, -1)).toBe(opened);

    // No loaded patch means nothing to navigate, not an index into nowhere.
    const unfetched = openedState();
    expect(moveDiffHunk(unfetched, 1)).toBe(unfetched);
  });

  it('closes to null and is inert while already closed', () => {
    const opened = openedState();
    const closed = closeDiffViewer(opened);
    expect(closed.diffViewer).toBeNull();
    expect(closeDiffViewer(closed)).toBe(closed);
    expect(moveDiffFile(closed, 1)).toBe(closed);
    expect(moveDiffHunk(closed, 1)).toBe(closed);
  });

  it('is cleared by the same reducers that clear the other overlays', () => {
    expect(closeOverlays(openedState()).diffViewer).toBeNull();
    expect(showLive(openedState()).diffViewer).toBeNull();
  });
});

describe('patch slots', () => {
  it('claims a loading slot once; the slot is the in-flight guard', () => {
    const first = markDiffPatchLoading(openedState(), 'src/lib.rs');
    expect(viewerOf(first).patches['src/lib.rs']).toEqual({kind: 'loading'});
    // A second mark is a no-op, so a revisited file cannot be fetched twice.
    expect(markDiffPatchLoading(first, 'src/lib.rs')).toBe(first);
  });

  it('resolves the loading slot with the patch, null patch and truncation kept', () => {
    const loaded = viewerOf(loadedState()).patches['src/lib.rs'];
    expect(loaded).toEqual({kind: 'loaded', patch: PATCH, truncated: false});

    // An absent patch is the repository failing to produce text, not an empty
    // diff; the slot keeps the distinction for the renderer.
    const state = markDiffPatchLoading(openedState(), 'src/lib.rs');
    const unavailable = applyDiffPatch(state, designPatch({}));
    expect(viewerOf(unavailable).patches['src/lib.rs']).toEqual({
      kind: 'loaded',
      patch: null,
      truncated: false,
    });
  });

  it('records a failed query against the file, not a global banner', () => {
    const state = markDiffPatchLoading(openedState(), 'src/lib.rs');
    const failed = failDiffPatch(state, designPatch({}), 'The request failed.');
    expect(viewerOf(failed).patches['src/lib.rs']).toEqual({
      kind: 'error',
      message: 'The request failed.',
    });
  });

  it('drops responses whose request the current viewer did not send', () => {
    const state = markDiffPatchLoading(openedState(), 'src/lib.rs');
    const patch = designPatch({patch: PATCH});

    // Reopened on another round's range while the query was in flight.
    const otherRange = designPatch({patch: PATCH, base: 'ccc3333'});
    expect(applyDiffPatch(state, otherRange)).toBe(state);
    expect(failDiffPatch(state, otherRange, 'stale')).toBe(state);

    // Closed entirely: nowhere to land.
    const closed = closeDiffViewer(state);
    expect(applyDiffPatch(closed, patch)).toBe(closed);

    // Already resolved: the answer that arrived first wins.
    const resolved = applyDiffPatch(state, patch);
    expect(applyDiffPatch(resolved, designPatch({patch: 'other'}))).toBe(resolved);
  });
});

describe('diffViewerLines', () => {
  it('renders the loaded patch with git line grammar tones under the file header', () => {
    const lines = diffViewerLines(viewerOf(loadedState()));
    expect(lines[0]).toEqual({text: '~ src/lib.rs', tone: 'meta'});
    expect(lines[1]).toEqual({text: '', tone: 'context'});
    const tones = lines.slice(2).map(line => line.tone);
    expect(tones).toEqual([
      'meta', // diff --git
      'meta', // index
      'meta', // ---
      'meta', // +++
      'hunk',
      'context',
      'add',
      'context',
      'hunk',
      'remove',
      'add',
    ]);
  });

  it('says a patch was truncated and names the exact external command', () => {
    const lines = diffViewerLines(viewerOf(loadedState(PATCH, true)));
    const texts = lines.map(line => line.text);
    expect(texts).toContain("Patch truncated at the server's size bound.");
    expect(texts).toContain('Full patch: git diff aaa1111 bbb2222 -- src/lib.rs');
    expect(lines.at(-1)?.tone).toBe('notice');
  });

  it('shows the query error against the file with the external fallback', () => {
    const failed = failDiffPatch(
      markDiffPatchLoading(openedState(), 'src/lib.rs'),
      designPatch({}),
      'The run has not attached yet.',
    );
    const texts = diffViewerLines(viewerOf(failed)).map(line => line.text);
    expect(texts).toContain('The patch query failed.');
    expect(texts).toContain('The run has not attached yet.');
    expect(texts).toContain('View it outside the TUI: git diff aaa1111 bbb2222 -- src/lib.rs');
  });

  it('explains a null patch as the repository going away, not an empty diff', () => {
    const unavailable = applyDiffPatch(
      markDiffPatchLoading(openedState(), 'src/lib.rs'),
      designPatch({}),
    );
    const texts = diffViewerLines(viewerOf(unavailable)).map(line => line.text);
    expect(texts).toContain('The workspace repository could not produce this patch.');
    expect(texts).toContain('From the workspace checkout: git diff aaa1111 bbb2222 -- src/lib.rs');
  });

  it('shows a loading row until the slot resolves', () => {
    const texts = diffViewerLines(viewerOf(openedState())).map(line => line.text);
    expect(texts).toEqual(['~ src/lib.rs', '', 'Loading patch…']);
  });

  it('names both paths of a rename in the external command', () => {
    const viewer = viewerOf(moveDiffFile(moveDiffFile(openedState(), 1), 1));
    const file = viewer.files[viewer.index];
    if (file === undefined) throw new Error('fixture has a third file');
    expect(diffExternalCommand(viewer, file)).toBe(
      'git diff aaa1111 bbb2222 -- src/old.rs src/moved.rs',
    );
  });
});

describe('hunk geometry and title', () => {
  it('maps the selected hunk to its on-screen row past the two header rows', () => {
    expect(diffHunkLines(PATCH)).toEqual([4, 8]);
    const opened = loadedState();
    expect(diffHunkDisplayLine(viewerOf(opened))).toBe(6);
    expect(diffHunkDisplayLine(viewerOf(moveDiffHunk(opened, 1)))).toBe(10);
    // Nothing loaded, nothing to scroll to.
    expect(diffHunkDisplayLine(viewerOf(openedState()))).toBeNull();
  });

  it('titles the box with the round, the file, and its place in the list', () => {
    expect(diffViewerTitle(viewerOf(openedState()))).toBe('Diff · Round 3 · src/lib.rs (1/3)');
  });
});
