import {existsSync, mkdirSync, readFileSync, writeFileSync} from 'node:fs';
import {homedir} from 'node:os';
import {join} from 'node:path';

/**
 * The operator's private notepad for one run, persisted outside
 * `run-events.jsonl`. The journal is the shared, replayable record of what
 * happened; a note is neither: it is scratch space that never reaches an
 * agent unless the operator explicitly promotes it into a composer and sends
 * it (see `session-model.ts#notepadPromotionText` and the two
 * `promoteNoteTo*` controller methods). Keeping it in its own file means a
 * bundle export, a replay, or a `run-events.jsonl` reader never sees it.
 */
export interface NoteRecord {
  readonly runId: string;
  readonly text: string;
  readonly createdAt: string;
  readonly updatedAt: string;
}

/**
 * Machine-local state root, matching the backend's own convention
 * (`docs/cli-flags.md`): `~/.vibesys`, overridable with `VIBESYS_STATE_HOME`
 * for tests and multi-checkout setups. Notes live under a `tui/notes/`
 * subtree the frontend owns outright, rather than inside the backend's
 * `projects/<project-key>/runs/<run-id>/` tree: the frontend never computes a
 * project key (it only ever speaks to the backend over the control socket,
 * per `launcher.ts`), and a note is frontend-only state, so it does not
 * belong under a path the backend also writes.
 */
function stateHome(): string {
  const override = process.env['VIBESYS_STATE_HOME'];
  return override !== undefined && override !== '' ? override : join(homedir(), '.vibesys');
}

/** Run ids are opaque strings; this keeps one confined to a single path segment. */
function sanitizeRunId(runId: string): string {
  return runId.replace(/[^a-zA-Z0-9_.-]/g, '_');
}

export function notePath(runId: string): string {
  return join(stateHome(), 'tui', 'notes', `${sanitizeRunId(runId)}.json`);
}

/**
 * Reads back the note for a run, or `null` if there is none yet, the file is
 * unreadable, or it does not parse as a `NoteRecord`. A corrupt or foreign
 * file degrades to "no note" rather than throwing: the notepad is a
 * convenience, not part of the run's correctness surface.
 */
export function readNote(runId: string): NoteRecord | null {
  const path = notePath(runId);
  if (!existsSync(path)) return null;
  try {
    const parsed: unknown = JSON.parse(readFileSync(path, 'utf8'));
    return isNoteRecord(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

/** Overwrites the note for `record.runId`, creating the notes directory on first use. */
export function writeNote(record: NoteRecord): void {
  const path = notePath(record.runId);
  mkdirSync(join(stateHome(), 'tui', 'notes'), {recursive: true});
  writeFileSync(path, JSON.stringify(record, null, 2));
}

function isNoteRecord(value: unknown): value is NoteRecord {
  if (typeof value !== 'object' || value === null) return false;
  const candidate = value as Record<string, unknown>;
  return (
    typeof candidate['runId'] === 'string' &&
    typeof candidate['text'] === 'string' &&
    typeof candidate['createdAt'] === 'string' &&
    typeof candidate['updatedAt'] === 'string'
  );
}
