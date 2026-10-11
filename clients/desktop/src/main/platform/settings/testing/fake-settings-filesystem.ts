/** An in-memory settings filesystem with explicit read faults and an operation record. */
import type {SettingsFilesystem} from '../settings-store.js';

export type SettingsFilesystemOperation =
  | {readonly kind: 'read'; readonly path: string}
  | {readonly kind: 'write'; readonly path: string}
  | {readonly kind: 'remove'; readonly path: string}
  | {readonly kind: 'replace'; readonly staging: string; readonly target: string};

export class FakeSettingsFilesystem implements SettingsFilesystem {
  readonly operations: SettingsFilesystemOperation[] = [];
  readonly #files = new Map<string, string>();
  readonly #readFailures = new Map<string, unknown[]>();

  constructor(files: Readonly<Record<string, string>> = {}) {
    for (const [path, text] of Object.entries(files)) this.#files.set(path, text);
  }

  failNextRead(path: string, error: unknown): void {
    const failures = this.#readFailures.get(path) ?? [];
    failures.push(error);
    this.#readFailures.set(path, failures);
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
    this.#files.set(path, text);
  }

  async remove(path: string): Promise<void> {
    this.operations.push({kind: 'remove', path});
    if (!this.#files.delete(path)) throw fileError('ENOENT', path);
  }

  async replace(staging: string, target: string): Promise<void> {
    this.operations.push({kind: 'replace', staging, target});
    const text = this.text(staging);
    this.#files.set(target, text);
    this.#files.delete(staging);
  }
}

export function fileError(code: string, path: string): Error {
  return Object.assign(new Error(`${code}: ${path}`), {code});
}
