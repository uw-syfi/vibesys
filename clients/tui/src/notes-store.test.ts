import {afterEach, beforeEach, describe, expect, it} from 'bun:test';
import {mkdtempSync, readFileSync, rmSync, writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {notePath, readNote, writeNote} from './notes-store.js';

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
    expect(readNote('run-1')).toBeNull();
  });

  it('round-trips a note through a write and a fresh read, as a restart would see it', () => {
    writeNote({runId: 'run-1', text: 'watch the retry budget', createdAt: 't0', updatedAt: 't0'});

    // A fresh read call, with nothing cached: this is what a new TUI process
    // attached to the same run would do on boot.
    const reread = readNote('run-1');
    expect(reread).toEqual({
      runId: 'run-1',
      text: 'watch the retry budget',
      createdAt: 't0',
      updatedAt: 't0',
    });
  });

  it("scopes notes by run id, so one run never sees another run's text", () => {
    writeNote({runId: 'run-a', text: 'note for run a', createdAt: 't0', updatedAt: 't0'});
    writeNote({runId: 'run-b', text: 'note for run b', createdAt: 't0', updatedAt: 't0'});

    expect(readNote('run-a')?.text).toBe('note for run a');
    expect(readNote('run-b')?.text).toBe('note for run b');
  });

  it("overwrites the same run's note rather than appending", () => {
    writeNote({runId: 'run-1', text: 'first draft', createdAt: 't0', updatedAt: 't0'});
    writeNote({runId: 'run-1', text: 'revised', createdAt: 't0', updatedAt: 't1'});

    expect(readNote('run-1')).toEqual({
      runId: 'run-1',
      text: 'revised',
      createdAt: 't0',
      updatedAt: 't1',
    });
  });

  it('sanitizes a run id with path-hostile characters instead of writing outside the notes directory', () => {
    const runId = '../../etc/evil';
    writeNote({runId, text: 'x', createdAt: 't0', updatedAt: 't0'});

    const path = notePath(runId);
    expect(path.startsWith(join(tempDir, 'tui', 'notes'))).toBe(true);
    expect(readNote(runId)?.text).toBe('x');
  });

  it('degrades to no note instead of throwing on a corrupt file', () => {
    const path = notePath('run-1');
    writeNote({runId: 'run-1', text: 'placeholder', createdAt: 't0', updatedAt: 't0'});
    writeFileSync(path, 'not json');

    expect(readNote('run-1')).toBeNull();
  });

  it('degrades to no note instead of throwing on a well-formed JSON file of the wrong shape', () => {
    const path = notePath('run-1');
    writeNote({runId: 'run-1', text: 'placeholder', createdAt: 't0', updatedAt: 't0'});
    writeFileSync(path, JSON.stringify({unrelated: true}));

    expect(readNote('run-1')).toBeNull();
  });

  it('respects VIBESYS_STATE_HOME for where the note file lands on disk', () => {
    writeNote({runId: 'run-1', text: 'on disk', createdAt: 't0', updatedAt: 't0'});

    const path = notePath('run-1');
    expect(path.startsWith(tempDir)).toBe(true);
    const onDisk: unknown = JSON.parse(readFileSync(path, 'utf8'));
    expect(onDisk).toMatchObject({runId: 'run-1', text: 'on disk'});
  });
});
