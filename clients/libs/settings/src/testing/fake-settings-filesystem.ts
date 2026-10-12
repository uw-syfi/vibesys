/** An in-memory settings filesystem with explicit read faults and an operation record. */
import type {SettingsFilesystem} from '../settings-store.js';

export type SettingsFilesystemOperation =
  | {readonly kind: 'read'; readonly path: string}
  | {readonly kind: 'write'; readonly path: string}
  | {readonly kind: 'remove'; readonly path: string}
  | {readonly kind: 'replace'; readonly staging: string; readonly target: string};

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

export class FakeSettingsFilesystem implements SettingsFilesystem {
  readonly operations: SettingsFilesystemOperation[] = [];
  readonly #files = new Map<string, string>();
  readonly #readFailures = new Map<string, unknown[]>();
  readonly #replaceFailures = new Map<string, unknown[]>();
  #nextWrite: WriteGate | null = null;
  #nextReplace: WriteGate | null = null;

  constructor(files: Readonly<Record<string, string>> = {}) {
    for (const [path, text] of Object.entries(files)) this.#files.set(path, text);
  }

  failNextRead(path: string, error: unknown): void {
    const failures = this.#readFailures.get(path) ?? [];
    failures.push(error);
    this.#readFailures.set(path, failures);
  }

  /** Make the next `replace()` targeting `path` throw `error` instead of committing. */
  failNextReplace(path: string, error: unknown): void {
    const failures = this.#replaceFailures.get(path) ?? [];
    failures.push(error);
    this.#replaceFailures.set(path, failures);
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
    const text = this.#files.get(path);
    if (text === undefined) throw fileError('ENOENT', path);
    return text;
  }

  async readText(path: string): Promise<string> {
    this.operations.push({kind: 'read', path});
    const failures = this.#readFailures.get(path);
    const failure = failures?.shift();
    if (failure !== undefined) throw failure;
    return this.text(path);
  }

  async writePrivateText(path: string, text: string): Promise<void> {
    this.operations.push({kind: 'write', path});
    const gate = this.#nextWrite;
    this.#nextWrite = null;
    if (gate !== null) {
      gate.arrive();
      await gate.released;
    }
    this.#files.set(path, text);
  }

  async remove(path: string): Promise<void> {
    this.operations.push({kind: 'remove', path});
    if (!this.#files.delete(path)) throw fileError('ENOENT', path);
  }

  async replace(staging: string, target: string): Promise<void> {
    this.operations.push({kind: 'replace', staging, target});
    const failures = this.#replaceFailures.get(target);
    const failure = failures?.shift();
    if (failure !== undefined) throw failure;
    const text = this.text(staging);
    this.#files.set(target, text);
    this.#files.delete(staging);
    const gate = this.#nextReplace;
    this.#nextReplace = null;
    if (gate !== null) {
      gate.arrive();
      await gate.released;
    }
  }
}

export function fileError(code: string, path: string): Error {
  return Object.assign(new Error(`${code}: ${path}`), {code});
}
