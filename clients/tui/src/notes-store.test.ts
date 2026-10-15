import {afterEach, beforeEach, describe, expect, it} from 'bun:test';
import {
  chmodSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  rmSync,
  statSync,
  symlinkSync,
  writeFileSync,
} from 'node:fs';
import {tmpdir} from 'node:os';
import {dirname, join} from 'node:path';
import {notePath, readNote, writeNote} from './notes-store.js';

const PROJECT_A = 'a'.repeat(64);
const PROJECT_B = 'b'.repeat(64);
const RUN_1 = {projectScope: PROJECT_A, runId: 'run-1'};

let tempDir: string;
let stateHomeDir: string;
const savedStateHome = process.env['VIBESYS_STATE_HOME'];

beforeEach(() => {
  tempDir = mkdtempSync(join(tmpdir(), 'vs-notes-store-test-'));
  stateHomeDir = join(tempDir, 'state-home');
  process.env['VIBESYS_STATE_HOME'] = stateHomeDir;
});

afterEach(() => {
  if (savedStateHome === undefined) delete process.env['VIBESYS_STATE_HOME'];
  else process.env['VIBESYS_STATE_HOME'] = savedStateHome;
  rmSync(tempDir, {recursive: true, force: true});
});

describe('notes-store', () => {
  it('has no note for a run before one is ever written', () => {
    expect(readNote(RUN_1)).toBeNull();
  });

  it('round-trips a note through a write and a fresh read, as a restart would see it', () => {
    writeNote({...RUN_1, text: 'watch the retry budget', createdAt: 't0', updatedAt: 't0'});

    // A fresh read call, with nothing cached: this is what a new TUI process
    // attached to the same run would do on boot.
    const reread = readNote(RUN_1);
    expect(reread).toEqual({
      projectScope: PROJECT_A,
      runId: 'run-1',
      text: 'watch the retry budget',
      createdAt: 't0',
      updatedAt: 't0',
    });
  });

  it('leaves a shared state root unchanged and makes only the note subtree owner-only', () => {
    mkdirSync(stateHomeDir);
    if (process.platform !== 'win32') chmodSync(stateHomeDir, 0o755);

    writeNote({...RUN_1, text: 'private draft', createdAt: 't0', updatedAt: 't0'});

    expect(readNote(RUN_1)?.text).toBe('private draft');
    if (process.platform === 'win32') return;

    const path = notePath(RUN_1);
    const projectDirectory = dirname(path);
    const notesDirectory = dirname(projectDirectory);
    const tuiDirectory = dirname(notesDirectory);
    expect(statSync(stateHomeDir).mode & 0o777).toBe(0o755);
    expect(statSync(tuiDirectory).mode & 0o777).toBe(0o700);
    expect(statSync(notesDirectory).mode & 0o777).toBe(0o700);
    expect(statSync(projectDirectory).mode & 0o777).toBe(0o700);
    expect(statSync(path).mode & 0o777).toBe(0o600);
  });

  it('repairs permissive note permissions when replacing an existing draft', () => {
    const path = notePath(RUN_1);
    const projectDirectory = dirname(path);
    const notesDirectory = dirname(projectDirectory);
    const tuiDirectory = dirname(notesDirectory);
    mkdirSync(projectDirectory, {recursive: true});
    writeFileSync(path, 'old draft');

    // chmod after creation makes this reproduction independent of the test runner's umask.
    if (process.platform !== 'win32') {
      chmodSync(tuiDirectory, 0o777);
      chmodSync(notesDirectory, 0o777);
      chmodSync(projectDirectory, 0o777);
      chmodSync(path, 0o666);
    }

    writeNote({...RUN_1, text: 'replacement', createdAt: 't0', updatedAt: 't1'});

    expect(readNote(RUN_1)?.text).toBe('replacement');
    if (process.platform === 'win32') return;
    expect(statSync(tuiDirectory).mode & 0o777).toBe(0o700);
    expect(statSync(notesDirectory).mode & 0o777).toBe(0o700);
    expect(statSync(projectDirectory).mode & 0o777).toBe(0o700);
    expect(statSync(path).mode & 0o777).toBe(0o600);
  });

  it('rejects a replaceable state root instead of claiming or changing it', () => {
    if (process.platform === 'win32') return;
    mkdirSync(stateHomeDir);
    chmodSync(stateHomeDir, 0o777);

    expect(() =>
      writeNote({...RUN_1, text: 'private draft', createdAt: 't0', updatedAt: 't0'}),
    ).toThrow('VIBESYS_STATE_HOME parent lets another user replace private note storage');

    expect(readNote(RUN_1)).toBeNull();
    expect(statSync(stateHomeDir).mode & 0o777).toBe(0o777);
  });

  it('rejects a state home whose parent lets another user replace it', () => {
    if (process.platform === 'win32') return;
    const sharedParent = join(tempDir, 'shared-parent');
    const configuredHome = join(sharedParent, 'state-home');
    mkdirSync(configuredHome, {recursive: true});
    chmodSync(sharedParent, 0o777);
    process.env['VIBESYS_STATE_HOME'] = configuredHome;

    expect(() =>
      writeNote({...RUN_1, text: 'must not escape', createdAt: 't0', updatedAt: 't0'}),
    ).toThrow('VIBESYS_STATE_HOME parent lets another user replace private note storage');

    expect(existsSync(join(configuredHome, 'tui'))).toBe(false);
  });

  it('accepts a protected symlinked state home while hardening only its note subtree', () => {
    if (process.platform === 'win32') return;
    const target = join(tempDir, 'state-home-target');
    const configuredHome = join(tempDir, 'state-home-link');
    mkdirSync(target);
    chmodSync(target, 0o755);
    symlinkSync(target, configuredHome, 'dir');
    // A trailing separator must not make lstat/open follow the link before O_NOFOLLOW sees it.
    process.env['VIBESYS_STATE_HOME'] = `${configuredHome}/`;

    writeNote({...RUN_1, text: 'private draft', createdAt: 't0', updatedAt: 't0'});

    expect(statSync(target).mode & 0o777).toBe(0o755);
    expect(readNote(RUN_1)?.text).toBe('private draft');
    expect(statSync(join(target, 'tui')).mode & 0o777).toBe(0o700);
    expect(statSync(notePath(RUN_1)).mode & 0o777).toBe(0o600);
  });

  it('accepts a real state home below a benign symlinked ancestor', () => {
    const realParent = join(tempDir, 'real-parent');
    const linkedParent = join(tempDir, 'linked-parent');
    mkdirSync(realParent);
    symlinkSync(realParent, linkedParent, 'dir');
    process.env['VIBESYS_STATE_HOME'] = join(linkedParent, 'nested', 'state-home');

    writeNote({...RUN_1, text: 'private draft', createdAt: 't0', updatedAt: 't0'});

    expect(readNote(RUN_1)?.text).toBe('private draft');
    expect(existsSync(join(realParent, 'nested', 'state-home', 'tui', 'notes'))).toBe(true);
    if (process.platform === 'win32') return;
    expect(statSync(notePath(RUN_1)).mode & 0o777).toBe(0o600);
  });

  it('rejects a symlinked ancestor whose containing directory is replaceable', () => {
    if (process.platform === 'win32') return;
    const replaceableParent = join(tempDir, 'replaceable-parent');
    const target = join(tempDir, 'ancestor-target');
    const linked = join(replaceableParent, 'linked');
    mkdirSync(replaceableParent);
    chmodSync(replaceableParent, 0o777);
    mkdirSync(target);
    symlinkSync(target, linked, 'dir');
    process.env['VIBESYS_STATE_HOME'] = join(linked, 'state-home');

    expect(() =>
      writeNote({...RUN_1, text: 'must not escape', createdAt: 't0', updatedAt: 't0'}),
    ).toThrow('VIBESYS_STATE_HOME parent lets another user replace private note storage');

    expect(existsSync(join(target, 'state-home'))).toBe(false);
    expect(statSync(replaceableParent).mode & 0o777).toBe(0o777);
  });

  it('creates missing intermediate directories in an absolute state-home override', () => {
    const nestedStateHome = join(tempDir, 'missing', 'nested', 'state-home');
    process.env['VIBESYS_STATE_HOME'] = nestedStateHome;

    writeNote({...RUN_1, text: 'private draft', createdAt: 't0', updatedAt: 't0'});

    expect(readNote(RUN_1)?.text).toBe('private draft');
    expect(existsSync(join(nestedStateHome, 'tui', 'notes'))).toBe(true);
  });

  it('creates usable missing state-home parents under a fully restrictive umask', () => {
    if (process.platform === 'win32') return;
    const firstMissing = join(tempDir, 'missing');
    const nestedParent = join(firstMissing, 'nested');
    const nestedStateHome = join(nestedParent, 'state-home');
    process.env['VIBESYS_STATE_HOME'] = nestedStateHome;

    const previous = process.umask(0o777);
    try {
      writeNote({...RUN_1, text: 'private draft', createdAt: 't0', updatedAt: 't0'});
    } finally {
      process.umask(previous);
    }

    const path = notePath(RUN_1);
    const projectDirectory = dirname(path);
    const notesDirectory = dirname(projectDirectory);
    const tuiDirectory = dirname(notesDirectory);
    expect(readNote(RUN_1)?.text).toBe('private draft');
    expect(statSync(firstMissing).mode & 0o777).toBe(0o700);
    expect(statSync(nestedParent).mode & 0o777).toBe(0o700);
    expect(statSync(nestedStateHome).mode & 0o777).toBe(0o700);
    expect(statSync(tuiDirectory).mode & 0o777).toBe(0o700);
    expect(statSync(notesDirectory).mode & 0o777).toBe(0o700);
    expect(statSync(projectDirectory).mode & 0o777).toBe(0o700);
    expect(statSync(path).mode & 0o777).toBe(0o600);
    expect(readdirSync(projectDirectory)).toEqual(['run-1.json']);
  });

  it('rejects a symlinked notes root without changing or writing through its target', () => {
    if (process.platform === 'win32') return;
    const path = notePath(RUN_1);
    const projectDirectory = dirname(path);
    const notesDirectory = dirname(projectDirectory);
    const target = join(tempDir, 'notes-root-target');
    const marker = join(target, 'keep.txt');
    mkdirSync(target);
    writeFileSync(marker, 'unchanged');
    chmodSync(target, 0o755);
    mkdirSync(dirname(notesDirectory), {recursive: true});
    symlinkSync(target, notesDirectory, 'dir');

    expect(() =>
      writeNote({...RUN_1, text: 'must not escape', createdAt: 't0', updatedAt: 't0'}),
    ).toThrow();

    expect(statSync(target).mode & 0o777).toBe(0o755);
    expect(readFileSync(marker, 'utf8')).toBe('unchanged');
    expect(existsSync(join(target, PROJECT_A))).toBe(false);
  });

  it('rejects a symlinked project directory without changing or writing through its target', () => {
    if (process.platform === 'win32') return;
    const path = notePath(RUN_1);
    const projectDirectory = dirname(path);
    const notesDirectory = dirname(projectDirectory);
    const target = join(tempDir, 'project-target');
    const marker = join(target, 'keep.txt');
    mkdirSync(target);
    writeFileSync(marker, 'unchanged');
    chmodSync(target, 0o755);
    mkdirSync(notesDirectory, {recursive: true});
    symlinkSync(target, projectDirectory, 'dir');

    expect(() =>
      writeNote({...RUN_1, text: 'must not escape', createdAt: 't0', updatedAt: 't0'}),
    ).toThrow();

    expect(statSync(target).mode & 0o777).toBe(0o755);
    expect(readFileSync(marker, 'utf8')).toBe('unchanged');
    expect(existsSync(join(target, 'run-1.json'))).toBe(false);
  });

  it('creates and hardens each hierarchy component under a fully restrictive umask', () => {
    if (process.platform === 'win32') return;
    mkdirSync(stateHomeDir);
    chmodSync(stateHomeDir, 0o755);
    const previous = process.umask(0o777);
    try {
      writeNote({...RUN_1, text: 'private draft', createdAt: 't0', updatedAt: 't0'});
    } finally {
      process.umask(previous);
    }

    const path = notePath(RUN_1);
    const projectDirectory = dirname(path);
    const notesDirectory = dirname(projectDirectory);
    const tuiDirectory = dirname(notesDirectory);
    expect(readNote(RUN_1)?.text).toBe('private draft');
    expect(statSync(stateHomeDir).mode & 0o777).toBe(0o755);
    expect(statSync(tuiDirectory).mode & 0o777).toBe(0o700);
    expect(statSync(notesDirectory).mode & 0o777).toBe(0o700);
    expect(statSync(projectDirectory).mode & 0o777).toBe(0o700);
    expect(statSync(path).mode & 0o777).toBe(0o600);
  });

  it("scopes notes by run id, so one run never sees another run's text", () => {
    const runA = {projectScope: PROJECT_A, runId: 'run-a'};
    const runB = {projectScope: PROJECT_A, runId: 'run-b'};
    writeNote({...runA, text: 'note for run a', createdAt: 't0', updatedAt: 't0'});
    writeNote({...runB, text: 'note for run b', createdAt: 't0', updatedAt: 't0'});

    expect(readNote(runA)?.text).toBe('note for run a');
    expect(readNote(runB)?.text).toBe('note for run b');
  });

  it('keeps the same run id isolated across project scopes', () => {
    const projectA = {projectScope: PROJECT_A, runId: 'shared'};
    const projectB = {projectScope: PROJECT_B, runId: 'shared'};
    writeNote({...projectA, text: 'private to project A', createdAt: 't0', updatedAt: 't0'});

    expect(readNote(projectB)).toBeNull();

    writeNote({...projectB, text: 'private to project B', createdAt: 't1', updatedAt: 't1'});
    expect(readNote(projectA)?.text).toBe('private to project A');
    expect(readNote(projectB)?.text).toBe('private to project B');
  });

  it("overwrites the same run's note rather than appending", () => {
    writeNote({...RUN_1, text: 'first draft', createdAt: 't0', updatedAt: 't0'});
    writeNote({...RUN_1, text: 'revised', createdAt: 't0', updatedAt: 't1'});

    expect(readNote(RUN_1)).toEqual({
      projectScope: PROJECT_A,
      runId: 'run-1',
      text: 'revised',
      createdAt: 't0',
      updatedAt: 't1',
    });
  });

  it('sanitizes a run id with path-hostile characters instead of writing outside the notes directory', () => {
    const runId = '../../etc/evil';
    const identity = {projectScope: PROJECT_A, runId};
    writeNote({...identity, text: 'x', createdAt: 't0', updatedAt: 't0'});

    const path = notePath(identity);
    expect(path.startsWith(join(stateHomeDir, 'tui', 'notes'))).toBe(true);
    expect(readNote(identity)?.text).toBe('x');
  });

  it('degrades to no note instead of throwing on a corrupt file', () => {
    const path = notePath(RUN_1);
    writeNote({...RUN_1, text: 'placeholder', createdAt: 't0', updatedAt: 't0'});
    writeFileSync(path, 'not json');

    expect(readNote(RUN_1)).toBeNull();
  });

  it('degrades to no note instead of throwing on a well-formed JSON file of the wrong shape', () => {
    const path = notePath(RUN_1);
    writeNote({...RUN_1, text: 'placeholder', createdAt: 't0', updatedAt: 't0'});
    writeFileSync(path, JSON.stringify({unrelated: true}));

    expect(readNote(RUN_1)).toBeNull();
  });

  it('rejects a valid note whose embedded identity does not match its path', () => {
    writeNote({...RUN_1, text: 'project A', createdAt: 't0', updatedAt: 't0'});
    writeFileSync(
      notePath(RUN_1),
      JSON.stringify({
        projectScope: PROJECT_B,
        runId: 'run-1',
        text: 'foreign note',
        createdAt: 't0',
        updatedAt: 't0',
      }),
    );

    expect(readNote(RUN_1)).toBeNull();
  });

  it('does not claim an ambiguous legacy run-only note for a project', () => {
    const legacyDirectory = join(stateHomeDir, 'tui', 'notes');
    mkdirSync(legacyDirectory, {recursive: true});
    writeFileSync(
      join(legacyDirectory, 'run-1.json'),
      JSON.stringify({runId: 'run-1', text: 'unknown project', createdAt: 't0', updatedAt: 't0'}),
    );

    expect(readNote(RUN_1)).toBeNull();
  });

  it('respects VIBESYS_STATE_HOME for where the note file lands on disk', () => {
    writeNote({...RUN_1, text: 'on disk', createdAt: 't0', updatedAt: 't0'});

    const path = notePath(RUN_1);
    expect(path.startsWith(stateHomeDir)).toBe(true);
    const onDisk: unknown = JSON.parse(readFileSync(path, 'utf8'));
    expect(onDisk).toMatchObject({projectScope: PROJECT_A, runId: 'run-1', text: 'on disk'});
  });

  it('keeps a shared run id isolated across many project scopes', () => {
    const identities = Array.from({length: 32}, (_, index) => ({
      projectScope: index.toString(16).padStart(64, '0'),
      runId: 'shared',
    }));
    for (const [index, identity] of identities.entries()) {
      writeNote({...identity, text: `project ${index}`, createdAt: 't0', updatedAt: 't0'});
    }

    for (const [index, identity] of identities.entries()) {
      expect(readNote(identity)?.text).toBe(`project ${index}`);
    }
  });
});
