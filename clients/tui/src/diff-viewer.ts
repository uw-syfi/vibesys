import {
  DesignChange,
  type DesignFileChange,
  type DesignPatch,
  type DesignRound,
} from '@vibesys/backend-client';
import type {DiffPatchSlot, DiffViewerState, SessionState} from './session-model.js';
import {formatFileChange} from './ui/design-log.js';

/**
 * Reducers and folds for the per-round diff viewer.
 *
 * The state lives in `SessionState.diffViewer` (session-model.ts owns the
 * shape and clears it with the other overlays); everything that mutates or
 * reads that state is here, in the same pure style as the session reducers.
 * The viewer never touches a repository: the round's commit range and file
 * list come from the design log the server already published, and the patch
 * text comes back one `query.design_patch` at a time as files are visited.
 */

/** The commit range and change list a viewer opens on: a `DesignRound`, validated. */
export type DiffRoundRange = Pick<DiffViewerState, 'round' | 'base' | 'head' | 'files'>;

/**
 * A round's range as the viewer needs it, or null when the round cannot be
 * diffed: no resolved base, no recorded head commit, an unreadable file list,
 * or nothing changed. `diffRangeExplanation` words those cases.
 */
export function diffRoundRange(round: DesignRound): DiffRoundRange | null {
  const base = round.base ?? null;
  const head = round.commit ?? null;
  const files = round.files?.changes ?? null;
  if (base === null || head === null || files === null || files.length === 0) return null;
  return {round: round.round, base, head, files};
}

/** Why `diffRoundRange` returned null, worded for the detail overlay. */
export function diffRangeExplanation(round: DesignRound): string {
  const files = round.files?.changes ?? null;
  if (files !== null && files.length === 0) {
    return `Round ${round.round} changed no workspace files.`;
  }
  if (files === null) {
    return (
      `Round ${round.round}'s file changes are not recorded, so there is no patch to show. ` +
      'The workspace repository may no longer be readable.'
    );
  }
  return `Round ${round.round} has no commit range to diff.`;
}

export function openDiffViewer(state: SessionState, range: DiffRoundRange): SessionState {
  // The viewer layers over whatever detail overlay was up; keeping both would
  // leave two modals fighting over the same box.
  return {...state, overlay: null, diffViewer: {...range, index: 0, hunk: 0, patches: {}}};
}

export function closeDiffViewer(state: SessionState): SessionState {
  if (state.diffViewer === null) return state;
  return {...state, diffViewer: null};
}

/** `←`/`→`: the previous/next changed file, holding at either end of the list. */
export function moveDiffFile(state: SessionState, delta: number): SessionState {
  const viewer = state.diffViewer;
  if (viewer === null) return state;
  const index = clamp(viewer.index + delta, 0, viewer.files.length - 1);
  if (index === viewer.index) return state;
  // A new file starts at its first hunk; its patch cache entry, if any, stays.
  return {...state, diffViewer: {...viewer, index, hunk: 0}};
}

/** `↑`/`↓`: the previous/next hunk of the loaded patch, holding at either end. */
export function moveDiffHunk(state: SessionState, delta: number): SessionState {
  const viewer = state.diffViewer;
  if (viewer === null) return state;
  const hunks = currentDiffHunkCount(viewer);
  if (hunks === 0) return state;
  const hunk = clamp(viewer.hunk + delta, 0, hunks - 1);
  if (hunk === viewer.hunk) return state;
  return {...state, diffViewer: {...viewer, hunk}};
}

/**
 * Claims a path's slot before its query is sent. The slot's presence is what
 * keeps a revisited file from being fetched twice, so it has to appear
 * synchronously with the request, not when the answer lands.
 */
export function markDiffPatchLoading(state: SessionState, path: string): SessionState {
  const viewer = state.diffViewer;
  if (viewer === null || viewer.patches[path] !== undefined) return state;
  return {
    ...state,
    diffViewer: {...viewer, patches: {...viewer.patches, [path]: {kind: 'loading'}}},
  };
}

export function applyDiffPatch(state: SessionState, patch: DesignPatch): SessionState {
  return resolveDiffPatch(state, patch, {
    kind: 'loaded',
    patch: patch.patch ?? null,
    truncated: patch.truncated === true,
  });
}

export function failDiffPatch(
  state: SessionState,
  key: DiffPatchKey,
  message: string,
): SessionState {
  return resolveDiffPatch(state, key, {kind: 'error', message});
}

export interface DiffPatchKey {
  base: string;
  head: string;
  path: string;
}

/**
 * A response lands only in the loading slot its own request created: the
 * viewer may have been closed, or closed and reopened on another round's
 * range, while the query was in flight.
 */
function resolveDiffPatch(
  state: SessionState,
  key: DiffPatchKey,
  slot: DiffPatchSlot,
): SessionState {
  const viewer = state.diffViewer;
  if (viewer === null || viewer.base !== key.base || viewer.head !== key.head) return state;
  if (viewer.patches[key.path]?.kind !== 'loading') return state;
  return {
    ...state,
    diffViewer: {...viewer, patches: {...viewer.patches, [key.path]: slot}},
  };
}

export function currentDiffFile(viewer: DiffViewerState): DesignFileChange | null {
  return viewer.files[viewer.index] ?? null;
}

/** ` Diff · Round 3 · src/lib.rs (2/5) `, without the box's own padding. */
export function diffViewerTitle(viewer: DiffViewerState): string {
  const file = currentDiffFile(viewer);
  if (file === null) return `Diff · Round ${viewer.round}`;
  return `Diff · Round ${viewer.round} · ${file.path} (${viewer.index + 1}/${viewer.files.length})`;
}

/**
 * The exact command that reproduces the file's full patch outside the TUI.
 * Shown wherever the viewer cannot: a truncated patch, a failed query, or a
 * workspace the server could no longer read. A rename names both paths, the
 * same pathspecs the server hands its own `git diff`.
 */
export function diffExternalCommand(viewer: DiffViewerState, file: DesignFileChange): string {
  const paths = file.renamedFrom ? `${file.renamedFrom} ${file.path}` : file.path;
  return `git diff ${viewer.base} ${viewer.head} -- ${paths}`;
}

/** How one line of the viewer is toned; the view maps these onto the theme. */
export type DiffLineTone = 'add' | 'remove' | 'hunk' | 'meta' | 'context' | 'notice';

export interface DiffViewerLine {
  text: string;
  tone: DiffLineTone;
}

/**
 * Rows the current file contributes above its patch: the change-list header
 * line and the blank under it. `diffHunkDisplayLine` adds this to patch-line
 * indexes, so the two agree on where the patch starts by construction.
 */
const DIFF_HEADER_LINES = 2;

/**
 * The viewer's body, one entry per row. Rows are rendered unwrapped, so an
 * index here is a row on screen, which is what lets hunk navigation scroll by
 * line index. Every state the current file's slot can be in renders as text
 * that says what it is: loading, a failed query, a repository that could not
 * produce the patch, and a truncated patch each explain themselves, with the
 * external `git diff` command wherever the full text lives outside the TUI.
 */
export function diffViewerLines(viewer: DiffViewerState): DiffViewerLine[] {
  const file = currentDiffFile(viewer);
  if (file === null) return [{text: 'This round has no files to diff.', tone: 'notice'}];
  const lines: DiffViewerLine[] = [
    {text: formatFileChange(file), tone: fileHeaderTone(file.change)},
    {text: '', tone: 'context'},
  ];
  const slot = viewer.patches[file.path];
  if (slot === undefined || slot.kind === 'loading') {
    lines.push({text: 'Loading patch…', tone: 'notice'});
    return lines;
  }
  if (slot.kind === 'error') {
    lines.push(
      {text: 'The patch query failed.', tone: 'notice'},
      {text: slot.message, tone: 'notice'},
      {text: '', tone: 'context'},
      {text: `View it outside the TUI: ${diffExternalCommand(viewer, file)}`, tone: 'notice'},
    );
    return lines;
  }
  if (slot.patch === null) {
    lines.push(
      {text: 'The workspace repository could not produce this patch.', tone: 'notice'},
      {
        text: 'A recorded run keeps its file list, but the patch needs the repository.',
        tone: 'notice',
      },
      {text: '', tone: 'context'},
      {text: `From the workspace checkout: ${diffExternalCommand(viewer, file)}`, tone: 'notice'},
    );
    return lines;
  }
  for (const line of patchLines(slot.patch)) {
    lines.push({text: line, tone: diffLineTone(line)});
  }
  if (slot.truncated) {
    lines.push(
      {text: '', tone: 'context'},
      {text: "Patch truncated at the server's size bound.", tone: 'notice'},
      {text: `Full patch: ${diffExternalCommand(viewer, file)}`, tone: 'notice'},
    );
  }
  return lines;
}

/**
 * Row index of the current hunk's `@@` header in `diffViewerLines`' output,
 * or null while the file has no loaded patch to navigate.
 */
export function diffHunkDisplayLine(viewer: DiffViewerState): number | null {
  const patch = loadedPatch(viewer);
  if (patch === null) return null;
  const line = diffHunkLines(patch)[viewer.hunk];
  return line === undefined ? null : DIFF_HEADER_LINES + line;
}

/** Patch-line indexes of the `@@` hunk headers, in order. */
export function diffHunkLines(patch: string): number[] {
  const indexes: number[] = [];
  for (const [index, line] of patchLines(patch).entries()) {
    if (line.startsWith('@@')) indexes.push(index);
  }
  return indexes;
}

function currentDiffHunkCount(viewer: DiffViewerState): number {
  const patch = loadedPatch(viewer);
  return patch === null ? 0 : diffHunkLines(patch).length;
}

function loadedPatch(viewer: DiffViewerState): string | null {
  const file = currentDiffFile(viewer);
  const slot = file === null ? undefined : viewer.patches[file.path];
  if (slot === undefined || slot.kind !== 'loaded') return null;
  return slot.patch;
}

/** git's own line grammar: `+++`/`---` are file headers, not content lines. */
export function diffLineTone(line: string): DiffLineTone {
  if (line.startsWith('@@')) return 'hunk';
  if (line.startsWith('+++') || line.startsWith('---')) return 'meta';
  if (line.startsWith('+')) return 'add';
  if (line.startsWith('-')) return 'remove';
  return META_PREFIXES.some(prefix => line.startsWith(prefix)) ? 'meta' : 'context';
}

/** Extended-header lines `git diff` emits before the first hunk. */
const META_PREFIXES = [
  'diff --git ',
  'index ',
  'new file mode',
  'deleted file mode',
  'old mode',
  'new mode',
  'similarity index',
  'dissimilarity index',
  'rename from',
  'rename to',
  'copy from',
  'copy to',
  'Binary files ',
];

function fileHeaderTone(change: DesignFileChange['change']): DiffLineTone {
  if (change === DesignChange.ADDED) return 'add';
  if (change === DesignChange.DELETED) return 'remove';
  return 'meta';
}

/** The patch as rows, without the empty row a trailing newline would add. */
function patchLines(patch: string): string[] {
  const lines = patch.split('\n');
  if (lines.at(-1) === '') lines.pop();
  return lines;
}

function clamp(value: number, low: number, high: number): number {
  return Math.min(Math.max(value, low), Math.max(low, high));
}
