/** An in-memory settings filesystem with explicit faults, crash recovery, and an operation record. */

import {dirname} from 'node:path';
import type {DirectorySyncCapability, SettingsFilesystem} from '../settings-store.js';

export type SettingsFilesystemOperation =
  | {readonly kind: 'read'; readonly path: string}
  | {readonly kind: 'write'; readonly path: string}
  | {readonly kind: 'sync-file'; readonly path: string}
  | {readonly kind: 'remove'; readonly path: string}
  | {readonly kind: 'replace'; readonly staging: string; readonly target: string}
  | {readonly kind: 'sync-directory'; readonly path: string}
  | {readonly kind: 'directory-sync-unavailable'; readonly path: string}
  | {readonly kind: 'crash'};

export interface FakeSettingsFilesystemOptions {
  /** Match POSIX by default; use `unavailable` to model Node on Windows. */
  readonly directorySync?: DirectorySyncCapability['kind'];
}

/** One explicitly held Fake write, released by the test rather than a clock. */
export interface DeferredSettingsWrite {
  readonly received: Promise<void>;
  release(): void;
}

/** An atomic replacement already applied, with its async completion explicitly held. */
export interface DeferredSettingsReplace {
  readonly committed: Promise<void>;
  release(): void;
}

class WriteGate implements DeferredSettingsWrite {
  readonly received: Promise<void>;
  readonly released: Promise<void>;
  readonly #arrive: () => void;
  readonly #release: () => void;

  constructor() {
    let arrive = (): void => {};
    let release = (): void => {};
    this.received = new Promise(resolve => {
      arrive = resolve;
    });
    this.released = new Promise(resolve => {
      release = resolve;
    });
    this.#arrive = arrive;
    this.#release = release;
  }

  arrive(): void {
    this.#arrive();
  }

  release(): void {
    this.#release();
  }
}

interface FakeFile {
  readonly text: string | Uint8Array;
  /** Bytes saved by a successful file sync; null means a crash may lose the contents. */
  readonly durableText: string | Uint8Array | null;
}

export class FakeSettingsFilesystem implements SettingsFilesystem {
  readonly operations: SettingsFilesystemOperation[] = [];
  readonly #files = new Map<string, FakeFile>();
  readonly #durableNamespace = new Map<string, FakeFile>();
  readonly #readFailures = new Map<string, unknown[]>();
  readonly #fileSyncFailures = new Map<string, unknown[]>();
  readonly #replaceFailures = new Map<string, unknown[]>();
  readonly #directorySyncFailures = new Map<string, unknown[]>();
  readonly #directorySync: DirectorySyncCapability['kind'];
  #nextStaging = 0;
  #nextWrite: WriteGate | null = null;
  #nextReplace: WriteGate | null = null;

  constructor(
    files: Readonly<Record<string, string>> = {},
    options: FakeSettingsFilesystemOptions = {},
  ) {
    this.#directorySync = options.directorySync ?? 'available';
    for (const [path, text] of Object.entries(files)) this.setText(path, text);
  }

  failNextRead(path: string, error: unknown): void {
    const failures = this.#readFailures.get(path) ?? [];
    failures.push(error);
    this.#readFailures.set(path, failures);
  }

  /** Make the next staged-file sync for `target` fail after its volatile write. */
  failNextFileSync(target: string, error: unknown): void {
    const failures = this.#fileSyncFailures.get(target) ?? [];
    failures.push(error);
    this.#fileSyncFailures.set(target, failures);
  }

  /** Make the next `replaceAtomically()` targeting `path` fail before its rename. */
  failNextReplace(path: string, error: unknown): void {
    const failures = this.#replaceFailures.get(path) ?? [];
    failures.push(error);
    this.#replaceFailures.set(path, failures);
  }

  /** Make the next parent-directory sync for `target` fail after its atomic rename. */
  failNextDirectorySync(target: string, error: unknown): void {
    if (this.#directorySync === 'unavailable') {
      throw new Error('cannot fail a directory sync when the capability is unavailable');
    }
    const failures = this.#directorySyncFailures.get(target) ?? [];
    failures.push(error);
    this.#directorySyncFailures.set(target, failures);
  }

  /** Hold the next staged write until the test releases it. */
  deferNextWrite(): DeferredSettingsWrite {
    if (this.#nextWrite !== null) throw new Error('a settings write is already deferred');
    const gate = new WriteGate();
    this.#nextWrite = gate;
    return gate;
  }

  /** Hold the next replace's promise after its target bytes have atomically changed. */
  deferNextReplaceAfterCommit(): DeferredSettingsReplace {
    if (this.#nextReplace !== null) throw new Error('a settings replace is already deferred');
    const gate = new WriteGate();
    this.#nextReplace = gate;
    return {committed: gate.received, release: () => gate.release()};
  }

  text(path: string): string {
    const file = this.#files.get(path);
    if (file === undefined) throw fileError('ENOENT', path);
    if (typeof file.text !== 'string') throw new Error(`not a text file: ${path}`);
    return file.text;
  }

  async readText(path: string): Promise<string> {
    this.operations.push({kind: 'read', path});
    const failures = this.#readFailures.get(path);
    const failure = failures?.shift();
    if (failure !== undefined) throw failure;
    return this.text(path);
  }

  bytes(path: string): Uint8Array {
    const file = this.#files.get(path);
    if (file === undefined) throw fileError('ENOENT', path);
    return typeof file.text === 'string' ? new TextEncoder().encode(file.text) : file.text.slice();
  }

  async readBytes(path: string): Promise<Uint8Array> {
    this.operations.push({kind: 'read', path});
    const failure = this.#readFailures.get(path)?.shift();
    if (failure !== undefined) throw failure;
    return this.bytes(path);
  }

  stageDurablePrivateBytes(target: string, bytes: Uint8Array): Promise<string> {
    return this.#stage(target, bytes.slice());
  }

  async #stage(target: string, text: string | Uint8Array): Promise<string> {
    let path: string;
    do {
      path = `${target}.${this.#nextStaging}.tmp`;
      this.#nextStaging += 1;
    } while (this.#files.has(path));
    this.operations.push({kind: 'write', path});
    const gate = this.#nextWrite;
    this.#nextWrite = null;
    if (gate !== null) {
      gate.arrive();
      await gate.released;
    }
    this.#files.set(path, {text, durableText: null});
    this.operations.push({kind: 'sync-file', path});
    const failures = this.#fileSyncFailures.get(target);
    const failure = failures?.shift();
    if (failure !== undefined) {
      this.#files.delete(path);
      throw failure;
    }
    this.#files.set(path, {text, durableText: text});
    return path;
  }

  /** Replace a file's bytes directly when arranging a test's external filesystem state. */
  setText(path: string, text: string): void {
    const file = {text, durableText: text};
    this.#files.set(path, file);
    this.#durableNamespace.set(path, file);
  }

  setBytes(path: string, bytes: Uint8Array): void {
    const file = {text: bytes.slice(), durableText: bytes.slice()};
    this.#files.set(path, file);
    this.#durableNamespace.set(path, file);
  }

  /** Simulate a crash by discarding every unflushed content and namespace mutation. */
  crash(): void {
    this.operations.push({kind: 'crash'});
    this.#files.clear();
    for (const [path, file] of this.#durableNamespace) {
      const text = file.durableText;
      if (text !== null) this.#files.set(path, {text, durableText: text});
    }
  }

  async remove(path: string): Promise<void> {
    this.operations.push({kind: 'remove', path});
    if (!this.#files.delete(path)) throw fileError('ENOENT', path);
  }

  async replaceAtomically(staging: string, target: string): Promise<void> {
    this.operations.push({kind: 'replace', staging, target});
    const failures = this.#replaceFailures.get(target);
    const failure = failures?.shift();
    if (failure !== undefined) throw failure;
    const file = this.#files.get(staging);
    if (file === undefined) throw fileError('ENOENT', staging);
    this.#files.set(target, file);
    this.#files.delete(staging);
    const gate = this.#nextReplace;
    this.#nextReplace = null;
    if (gate !== null) {
      gate.arrive();
      await gate.released;
    }
    const directory = dirname(target);
    if (this.#directorySync === 'unavailable') {
      this.operations.push({kind: 'directory-sync-unavailable', path: directory});
      return;
    }
    this.operations.push({kind: 'sync-directory', path: directory});
    const syncFailures = this.#directorySyncFailures.get(target);
    const syncFailure = syncFailures?.shift();
    if (syncFailure !== undefined) throw syncFailure;
    this.#syncDirectory(directory);
  }

  #syncDirectory(directory: string): void {
    for (const path of this.#durableNamespace.keys()) {
      if (dirname(path) === directory) this.#durableNamespace.delete(path);
    }
    for (const [path, file] of this.#files) {
      if (dirname(path) !== directory) continue;
      this.#durableNamespace.set(path, {...file});
    }
  }
}

export function fileError(code: string, path: string): Error {
  return Object.assign(new Error(`${code}: ${path}`), {code});
}
