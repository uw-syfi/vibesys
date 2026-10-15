import {
  type BigIntStats,
  chmodSync,
  closeSync,
  constants,
  existsSync,
  fchmodSync,
  fstatSync,
  lstatSync,
  mkdirSync,
  openSync,
  readFileSync,
  realpathSync,
  renameSync,
  rmSync,
  writeFileSync,
} from 'node:fs';
import {homedir} from 'node:os';
import {basename, dirname, join, parse, resolve} from 'node:path';

const SHARED_DIRECTORY_MODE = 0o700;
const PRIVATE_DIRECTORY_MODE = 0o700;
const PRIVATE_FILE_MODE = 0o600;

/**
 * The operator's private notepad for one run, persisted outside
 * `run-events.jsonl`. The journal is the shared, replayable record of what
 * happened; a note is neither: it is scratch space that never reaches an
 * agent unless the operator explicitly promotes it into a composer and sends
 * it (see `session-model.ts#notepadPromotionText` and the two
 * `promoteNoteTo*` controller methods). Keeping it in its own file means a
 * bundle export, a replay, or a `run-events.jsonl` reader never sees it.
 */
export interface NoteIdentity {
  /** Opaque stable namespace supplied by the backend for one local project. */
  readonly projectScope: string;
  readonly runId: string;
}

export interface NoteRecord extends NoteIdentity {
  readonly text: string;
  readonly createdAt: string;
  readonly updatedAt: string;
}

/**
 * Machine-local state root, matching the backend's own convention
 * (`docs/cli-flags.md`): `~/.vibesys`, overridable with `VIBESYS_STATE_HOME`
 * for tests and multi-checkout setups. Notes live under a `tui/notes/`
 * subtree the frontend owns outright, namespaced by the backend's opaque
 * project scope rather than inside the backend's
 * `projects/<project-key>/runs/<run-id>/` tree: the frontend never computes a
 * project key (it only ever speaks to the backend over the control socket,
 * per `launcher.ts`), and a note is frontend-only state, so it does not
 * belong under a path the backend also writes.
 */
function stateHome(): string {
  const override = process.env['VIBESYS_STATE_HOME'];
  return resolve(
    override !== undefined && override !== '' ? override : join(homedir(), '.vibesys'),
  );
}

/** Run ids are opaque strings; this keeps one confined to a single path segment. */
function sanitizeSegment(value: string): string {
  return value.replace(/[^a-zA-Z0-9_.-]/g, '_');
}

interface NoteLocations {
  readonly home: string;
  readonly tui: string;
  readonly notes: string;
  readonly project: string;
  readonly file: string;
}

interface OpenDirectory {
  readonly path: string;
  readonly descriptor: number;
  readonly device: bigint;
  readonly inode: bigint;
}

interface PreparedNoteDirectories {
  readonly all: readonly OpenDirectory[];
  readonly project: OpenDirectory;
}

function noteLocations(identity: NoteIdentity): NoteLocations {
  const home = stateHome();
  const tui = join(home, 'tui');
  const notes = join(tui, 'notes');
  const project = join(notes, sanitizeSegment(identity.projectScope));
  return {
    home,
    tui,
    notes,
    project,
    file: join(project, `${sanitizeSegment(identity.runId)}.json`),
  };
}

function isAlreadyPresent(error: unknown): boolean {
  return typeof error === 'object' && error !== null && 'code' in error && error.code === 'EEXIST';
}

function isMissing(error: unknown): boolean {
  return typeof error === 'object' && error !== null && 'code' in error && error.code === 'ENOENT';
}

function currentUserId(): bigint {
  const id = process.geteuid?.() ?? process.getuid?.();
  if (id === undefined) {
    throw new Error('POSIX note storage requires an effective user id');
  }
  return BigInt(id);
}

function directoryAt(path: string): BigIntStats {
  const status = lstatSync(path, {bigint: true});
  if (!status.isDirectory()) {
    const error = new Error(
      `ENOTDIR: Private note path is not a directory: ${path}`,
    ) as NodeJS.ErrnoException;
    error.code = 'ENOTDIR';
    throw error;
  }
  return status;
}

function ownedDirectoryAt(path: string, userId: bigint): BigIntStats {
  const status = directoryAt(path);
  if (status.uid !== userId) {
    throw new Error(`Private note directory is not owned by the current user: ${path}`);
  }
  return status;
}

function openDirectory(path: string, expected: BigIntStats): OpenDirectory {
  const descriptor = openSync(
    path,
    constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW,
  );
  try {
    const status = fstatSync(descriptor, {bigint: true});
    if (!status.isDirectory() || status.dev !== expected.dev || status.ino !== expected.ino) {
      throw new Error(`Private note directory changed while it was being opened: ${path}`);
    }
    return {path, descriptor, device: status.dev, inode: status.ino};
  } catch (error) {
    closeSync(descriptor);
    throw error;
  }
}

function assertDirectoryStillNamed(directory: OpenDirectory): void {
  const descriptorStatus = fstatSync(directory.descriptor, {bigint: true});
  const pathStatus = directoryAt(directory.path);
  if (
    descriptorStatus.dev !== directory.device ||
    descriptorStatus.ino !== directory.inode ||
    pathStatus.dev !== directory.device ||
    pathStatus.ino !== directory.inode
  ) {
    throw new Error(`Private note directory changed during the write: ${directory.path}`);
  }
}

function isTrustedOwner(owner: bigint, userId: bigint, systemOwnerId: bigint): boolean {
  return owner === userId || owner === 0n || owner === systemOwnerId;
}

function unsafeStateHomeParent(path: string): Error {
  return new Error(
    `VIBESYS_STATE_HOME parent lets another user replace private note storage: ${path}`,
  );
}

function assertAncestorEntryProtected(
  entryPath: string,
  entryStatus: BigIntStats,
  userId: bigint,
  systemOwnerId: bigint,
): void {
  const containerPath = dirname(entryPath);
  if (containerPath === entryPath) return;

  const containerStatus = directoryAt(containerPath);
  if (!isTrustedOwner(containerStatus.uid, userId, systemOwnerId)) {
    throw unsafeStateHomeParent(containerPath);
  }
  const writableByAnotherUser = (containerStatus.mode & 0o022n) !== 0n;
  const sticky = (containerStatus.mode & 0o1000n) !== 0n;
  if (
    writableByAnotherUser &&
    (!sticky || !isTrustedOwner(entryStatus.uid, userId, systemOwnerId))
  ) {
    throw unsafeStateHomeParent(containerPath);
  }
  assertAncestorEntryProtected(containerPath, containerStatus, userId, systemOwnerId);
}

function assertSafeContainer(
  directory: OpenDirectory,
  userId: bigint,
  systemOwnerId: bigint,
): void {
  const status = fstatSync(directory.descriptor, {bigint: true});
  if (!isTrustedOwner(status.uid, userId, systemOwnerId)) {
    throw unsafeStateHomeParent(directory.path);
  }
  const writableByAnotherUser = (status.mode & 0o022n) !== 0n;
  const sticky = (status.mode & 0o1000n) !== 0n;
  if (writableByAnotherUser && !sticky) throw unsafeStateHomeParent(directory.path);
  assertAncestorEntryProtected(directory.path, status, userId, systemOwnerId);
}

/** Prove that every existing entry in a lexical path is protected before resolving its symlinks. */
function assertProtectedLexicalPath(path: string, userId: bigint, systemOwnerId: bigint): void {
  const root = parse(path).root;
  let entry = root;
  for (const name of path.slice(root.length).split('/').filter(Boolean)) {
    entry = join(entry, name);
    let entryStatus: BigIntStats;
    try {
      entryStatus = lstatSync(entry, {bigint: true});
    } catch (error) {
      if (isMissing(error)) return;
      throw error;
    }
    const containerPath = realpathSync(dirname(entry));
    const containerStatus = directoryAt(containerPath);
    if (!isTrustedOwner(containerStatus.uid, userId, systemOwnerId)) {
      throw unsafeStateHomeParent(containerPath);
    }
    const writableByAnotherUser = (containerStatus.mode & 0o022n) !== 0n;
    const sticky = (containerStatus.mode & 0o1000n) !== 0n;
    if (
      writableByAnotherUser &&
      (!sticky || !isTrustedOwner(entryStatus.uid, userId, systemOwnerId))
    ) {
      throw unsafeStateHomeParent(containerPath);
    }
  }
}

interface StateHomeAnchor {
  readonly path: string;
  readonly status: BigIntStats;
  readonly missing: readonly string[];
}

function stateHomeAnchor(path: string): StateHomeAnchor {
  const missing: string[] = [];
  let candidate = path;
  while (true) {
    try {
      lstatSync(candidate, {bigint: true});
      const canonical = realpathSync(candidate);
      return {path: canonical, status: directoryAt(canonical), missing};
    } catch (error) {
      if (!isMissing(error)) throw error;
      const parent = dirname(candidate);
      if (parent === candidate) throw error;
      missing.unshift(basename(candidate));
      candidate = parent;
    }
  }
}

function prepareSharedChild(
  parent: OpenDirectory,
  name: string,
  userId: bigint,
  systemOwnerId: bigint,
): OpenDirectory {
  assertDirectoryStillNamed(parent);
  const path = join(parent.path, name);
  let created = false;
  try {
    mkdirSync(path, {mode: SHARED_DIRECTORY_MODE});
    created = true;
  } catch (error) {
    if (!isAlreadyPresent(error)) throw error;
  }
  assertDirectoryStillNamed(parent);
  let status = directoryAt(path);
  if (created) {
    if (status.uid !== userId) throw unsafeStateHomeParent(path);
    // The retained parent proves this entry cannot be replaced by another user. Recover the exact
    // usable mode that mkdir requested before opening it, even when the process umask removed every
    // permission bit. Existing shared entries are deliberately never changed here.
    chmodSync(path, SHARED_DIRECTORY_MODE);
    assertDirectoryStillNamed(parent);
    status = ownedDirectoryAt(path, userId);
  }
  if (!isTrustedOwner(status.uid, userId, systemOwnerId)) {
    throw unsafeStateHomeParent(path);
  }
  const child = openDirectory(path, status);
  try {
    assertSafeContainer(child, userId, systemOwnerId);
    return child;
  } catch (error) {
    closeSync(child.descriptor);
    throw error;
  }
}

/**
 * Open the configured shared state home as the trust anchor for private notes.
 *
 * The root is shared with the backend, cache, and other clients, so this module never changes its
 * mode or requires it to be owned by the current user. It does require each existing lexical and
 * canonical ancestor to be controlled by the current user or the filesystem's system owner and to
 * protect the next pathname entry. Benign protected symlinks, including the configured state-home
 * entry itself, are resolved before opening the canonical anchor, matching the backend's path
 * contract without accepting a link that another user can replace.
 *
 * Missing override components are created one at a time as owner-only directories under a
 * retained and revalidated parent. Existing shared entries are never chmodded; newly created ones
 * recover the backend's 0700 state-home mode even when the process umask removes every bit.
 */
function prepareStateHome(path: string, userId: bigint): OpenDirectory {
  // User namespaces can map the system account away from numeric uid 0. The owner of the
  // filesystem root is the authority already able to replace any pathname on this filesystem.
  const systemOwnerId = directoryAt(parse(path).root).uid;
  assertProtectedLexicalPath(path, userId, systemOwnerId);
  const anchor = stateHomeAnchor(path);
  let current = openDirectory(anchor.path, anchor.status);
  const retained: OpenDirectory[] = [current];
  try {
    assertSafeContainer(current, userId, systemOwnerId);
    for (const name of anchor.missing) {
      current = prepareSharedChild(current, name, userId, systemOwnerId);
      retained.push(current);
    }
  } catch (error) {
    closeDirectories(retained);
    throw error;
  }
  const closeError = closeDirectories(retained.slice(0, -1));
  if (closeError === undefined) return current;
  closeDirectories([current]);
  throw closeError;
}

/** Create and retain one owner-only child below an already retained private parent. */
function preparePrivateChild(parent: OpenDirectory, name: string, userId: bigint): OpenDirectory {
  assertDirectoryStillNamed(parent);
  const path = join(parent.path, name);
  try {
    mkdirSync(path, {mode: PRIVATE_DIRECTORY_MODE});
  } catch (error) {
    if (!isAlreadyPresent(error)) throw error;
  }

  assertDirectoryStillNamed(parent);
  const status = ownedDirectoryAt(path, userId);
  // The retained parent has proved that another user cannot replace this owned entry. A path chmod
  // is needed only to recover descriptor access when creation (or an older client) left mode 0.
  chmodSync(path, PRIVATE_DIRECTORY_MODE);
  assertDirectoryStillNamed(parent);
  const directory = openDirectory(path, status);
  try {
    fchmodSync(directory.descriptor, PRIVATE_DIRECTORY_MODE);
    assertDirectoryStillNamed(directory);
    return directory;
  } catch (error) {
    closeSync(directory.descriptor);
    throw error;
  }
}

function closeDirectories(directories: readonly OpenDirectory[]): unknown {
  let firstError: unknown;
  for (let index = directories.length - 1; index >= 0; index -= 1) {
    const directory = directories[index];
    if (directory === undefined) continue;
    try {
      closeSync(directory.descriptor);
    } catch (error) {
      firstError ??= error;
    }
  }
  return firstError;
}

function prepareNoteDirectories(locations: NoteLocations): PreparedNoteDirectories {
  const userId = currentUserId();
  let parent = prepareStateHome(locations.home, userId);
  const directories: OpenDirectory[] = [parent];
  try {
    for (const name of ['tui', 'notes', basename(locations.project)]) {
      parent = preparePrivateChild(parent, name, userId);
      directories.push(parent);
    }
    return {all: directories, project: parent};
  } catch (error) {
    closeDirectories(directories);
    throw error;
  }
}

function assertHierarchyStillNamed(directories: readonly OpenDirectory[]): void {
  for (const directory of directories) assertDirectoryStillNamed(directory);
}

export function notePath(identity: NoteIdentity): string {
  return noteLocations(identity).file;
}

/**
 * Reads back the note for a run, or `null` if there is none yet, the file is
 * unreadable, or it does not parse as a `NoteRecord`. A corrupt or foreign
 * file degrades to "no note" rather than throwing: the notepad is a
 * convenience, not part of the run's correctness surface.
 */
export function readNote(identity: NoteIdentity): NoteRecord | null {
  const path = notePath(identity);
  if (!existsSync(path)) return null;
  try {
    const parsed: unknown = JSON.parse(readFileSync(path, 'utf8'));
    return isNoteRecord(parsed, identity) ? parsed : null;
  } catch {
    return null;
  }
}

/**
 * Atomically replaces one project's note for `record.runId`. The notes hierarchy and staged file
 * are made owner-only explicitly, so confidentiality does not depend on the process umask or the
 * inherited mode of a path created by an older client.
 */
export function writeNote(record: NoteRecord): void {
  const locations = noteLocations(record);
  const contents = JSON.stringify(record, null, 2);
  // Windows doesn't enforce POSIX ownership bits or expose the same no-follow directory-open
  // contract. Retain its existing recursive creation and direct replacement behavior explicitly.
  if (process.platform === 'win32') {
    mkdirSync(locations.project, {recursive: true});
    writeFileSync(locations.file, contents);
    return;
  }

  const prepared = prepareNoteDirectories(locations);
  const path = join(prepared.project.path, basename(locations.file));
  const staging = `${path}.${process.pid}.tmp`;
  let operationError: unknown;
  try {
    assertHierarchyStillNamed(prepared.all);
    // The shared state root is trusted and every retained TUI-owned descendant is owner-only. A
    // predictable stale name can therefore be removed before the exclusive create.
    rmSync(staging, {force: true});
    assertDirectoryStillNamed(prepared.project);
    const descriptor = openSync(
      staging,
      constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW,
      PRIVATE_FILE_MODE,
    );
    try {
      // Creation mode is filtered through the umask. The open descriptor remains usable even when
      // every bit was removed, so restore and populate the exact inode before publishing it.
      fchmodSync(descriptor, PRIVATE_FILE_MODE);
      writeFileSync(descriptor, contents);
    } finally {
      closeSync(descriptor);
    }
    assertHierarchyStillNamed(prepared.all);
    renameSync(staging, path);
    assertHierarchyStillNamed(prepared.all);
  } catch (error) {
    operationError = error;
    try {
      rmSync(staging, {force: true});
    } catch {
      // Best effort: cleanup must not hide the write or replace failure.
    }
  }
  const closeError = closeDirectories(prepared.all);
  if (operationError !== undefined) throw operationError;
  if (closeError !== undefined) throw closeError;
}

function isNoteRecord(value: unknown, identity: NoteIdentity): value is NoteRecord {
  if (typeof value !== 'object' || value === null) return false;
  const candidate = value as Record<string, unknown>;
  return (
    candidate['projectScope'] === identity.projectScope &&
    candidate['runId'] === identity.runId &&
    typeof candidate['text'] === 'string' &&
    typeof candidate['createdAt'] === 'string' &&
    typeof candidate['updatedAt'] === 'string'
  );
}
