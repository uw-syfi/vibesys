/** The Changes pane: a round's files, the state of each patch, and the command that reproduces it. */
import type {DesignFileChange, DesignPatch, DesignRound} from '@vibesys/backend-client';
import type {DiffLine} from './model.js';
import type {RoundRow} from './rounds.js';

export type PatchSlot =
  | {kind: 'loading'}
  | {kind: 'error'; message: string}
  | {kind: 'loaded'; patch: DesignPatch | null};

type FileBody =
  | {kind: 'loading'}
  | {kind: 'error'; message: string}
  /** The server answered no patch text: the workspace repository could not produce it. */
  | {kind: 'unavailable'}
  | {kind: 'patch'; lines: DiffLine[]; truncated: boolean};

export interface FileChanges {
  path: string;
  renamedFrom: string | null;
  change: DesignFileChange['change'];
  added: number;
  removed: number;
  body: FileBody;
  /** Reproduces the full patch outside the app. */
  command: string;
}

export type ChangesModel =
  | {kind: 'none'}
  | {kind: 'running'; round: number}
  | {kind: 'unresolved'; round: number}
  | {kind: 'files'; round: number; against: string; commit: string | null; files: FileChanges[]};

export interface PatchRequest {
  key: string;
  base: string;
  head: string;
  path: string;
}

export function patchKey(base: string, head: string, path: string): string {
  return `${base}..${head}:${path}`;
}

export type LoadPatch = (base: string, head: string, path: string) => Promise<DesignPatch | null>;

/**
 * Resolves one patch request to a slot. A rejection (the session's `designPatch` propagates the
 * request's rejection) settles as an error slot rather than an unhandled rejection or a stuck
 * loading state.
 */
export async function loadPatchSlot(load: LoadPatch, request: PatchRequest): Promise<PatchSlot> {
  try {
    return {kind: 'loaded', patch: await load(request.base, request.head, request.path)};
  } catch (error) {
    return {kind: 'error', message: error instanceof Error ? error.message : String(error)};
  }
}

/** One request per file, when the round's range has both ends. */
export function patchRequests(design: DesignRound | undefined): PatchRequest[] {
  const base = design?.base;
  const head = design?.commit;
  if (!base || !head || !design?.files) return [];
  return design.files.map(file => ({
    key: patchKey(base, head, file.path),
    base,
    head,
    path: file.path,
  }));
}

const SAFE = /^[\w@%+=:,./-]+$/;

/** POSIX single-quoting: a path with spaces or quotes pastes into a shell as one word. */
export function shellQuote(word: string): string {
  return SAFE.test(word) ? word : `'${word.replaceAll("'", `'\\''`)}'`;
}

/** A rename names both paths, the same pathspecs the server hands its own `git diff`. */
function reproCommand(design: DesignRound, file: DesignFileChange): string {
  const names = file.renamed_from ? [file.renamed_from, file.path] : [file.path];
  const paths = names.map(shellQuote).join(' ');
  if (design.base && design.commit)
    return `git diff ${shellQuote(design.base)} ${shellQuote(design.commit)} -- ${paths}`;
  return `git show ${shellQuote(design.commit ?? 'HEAD')} -- ${paths}`;
}

const HUNK = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/;

interface Counters {
  before: number;
  after: number;
}

/** Classifies one line of `git diff` output inside a hunk; null drops it (a blank trailer). */
function patchLine(text: string, counters: Counters): DiffLine | null {
  if (text === '') return null;
  if (text.startsWith('+')) return {tone: 'add', text: text.slice(1), line: counters.after++};
  if (text.startsWith('-')) return {tone: 'del', text: text.slice(1), line: counters.before++};
  if (text.startsWith('\\')) return {tone: 'meta', text, line: null};
  const line: DiffLine = {tone: 'ctx', text: text.slice(1), line: counters.after};
  counters.before += 1;
  counters.after += 1;
  return line;
}

/** `git diff` output for one file as rows; the extended header before the first hunk is dropped. */
export function parsePatch(patch: string): DiffLine[] {
  const lines: DiffLine[] = [];
  const counters: Counters = {before: 0, after: 0};
  let inHunk = false;
  for (const text of patch.replace(/\n$/, '').split('\n')) {
    const hunk = HUNK.exec(text);
    if (hunk) {
      counters.before = Number(hunk[1]);
      counters.after = Number(hunk[2]);
      inHunk = true;
      lines.push({tone: 'hunk', text, line: null});
      continue;
    }
    if (!inHunk) continue;
    const line = patchLine(text, counters);
    if (line !== null) lines.push(line);
  }
  return lines;
}

function bodyOf(slot: PatchSlot | undefined): FileBody {
  if (slot === undefined || slot.kind === 'loading') return {kind: 'loading'};
  if (slot.kind === 'error') return {kind: 'error', message: slot.message};
  const text = slot.patch?.patch;
  if (text === null || text === undefined) return {kind: 'unavailable'};
  return {kind: 'patch', lines: parsePatch(text), truncated: slot.patch?.truncated === true};
}

function fileChanges(
  design: DesignRound,
  file: DesignFileChange,
  patches: Readonly<Record<string, PatchSlot>>,
): FileChanges {
  const queryable = Boolean(design.base && design.commit);
  const slot: PatchSlot | undefined = queryable
    ? patches[patchKey(design.base ?? '', design.commit ?? '', file.path)]
    : {kind: 'loaded', patch: null};
  const body = bodyOf(slot);
  const lines = body.kind === 'patch' ? body.lines : [];
  return {
    path: file.path,
    renamedFrom: file.renamed_from ?? null,
    change: file.change,
    added: lines.filter(line => line.tone === 'add').length,
    removed: lines.filter(line => line.tone === 'del').length,
    body,
    command: reproCommand(design, file),
  };
}

export function changesModel(
  row: RoundRow | undefined,
  design: DesignRound | undefined,
  patches: Readonly<Record<string, PatchSlot>>,
): ChangesModel {
  if (row === undefined) return {kind: 'none'};
  if (row.state === 'running' || row.state === 'paused') return {kind: 'running', round: row.round};
  if (design?.files == null) return {kind: 'unresolved', round: row.round};
  return {
    kind: 'files',
    round: row.round,
    against: row.before.round === 0 ? 'the baseline' : `the round ${row.before.round} checkpoint`,
    commit: design.commit ?? null,
    files: design.files.map(file => fileChanges(design, file, patches)),
  };
}
