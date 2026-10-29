import type {RunRecords} from './effects/controls.js';

/**
 * The records of a run's one reader, as its fanout delivers them to the intent history. The test
 * says how a record is encoded, since the reader hands this feature the encoded form.
 */
export class FakeRecords<Record> {
  readonly #listeners = new Set<(records: readonly Uint8Array[]) => void>();
  readonly #encode: (record: Record) => Uint8Array;

  constructor(encode: (record: Record) => Uint8Array) {
    this.#encode = encode;
  }

  readonly subscribe: RunRecords = listener => {
    this.#listeners.add(listener);
    return () => {
      this.#listeners.delete(listener);
    };
  };

  get subscribers(): number {
    return this.#listeners.size;
  }

  publish(records: readonly Record[]): void {
    const bytes = records.map(this.#encode);
    for (const listener of [...this.#listeners]) listener(bytes);
  }
}
