import {afterEach, beforeEach, describe, expect, it} from 'bun:test';
import {mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {notePath, readNote, writeNote} from './notes-store.js';

const PROJECT_A = 'a'.repeat(64);
const PROJECT_B = 'b'.repeat(64);
const RUN_1 = {projectScope: PROJECT_A, runId: 'run-1'};

let tempDir: string;
const savedStateHome = process.env['VIBESYS_STATE_HOME'];

beforeEach(() => {
  tempDir = mkdtempSync(join(tmpdir(), 'vs-notes-store-test-'));
  process.env['VIBESYS_STATE_HOME'] = tempDir;
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
    expect(path.startsWith(join(tempDir, 'tui', 'notes'))).toBe(true);
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
    const legacyDirectory = join(tempDir, 'tui', 'notes');
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
    expect(path.startsWith(tempDir)).toBe(true);
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
