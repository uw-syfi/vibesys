import {toBinary} from '@bufbuild/protobuf';
import {RecordSchema, type Record as StoreRecord} from '@vibesys/proto';
import type {RunRecords} from './effects/controls.js';

/** The records of a run's one reader, as its fanout delivers them to the intent history. */
export class FakeRecords {
  readonly #listeners = new Set<(records: readonly Uint8Array[]) => void>();

  readonly subscribe: RunRecords = listener => {
    this.#listeners.add(listener);
    return () => {
      this.#listeners.delete(listener);
    };
  };

  get subscribers(): number {
    return this.#listeners.size;
  }

  publish(records: readonly StoreRecord[]): void {
    const bytes = records.map(record => toBinary(RecordSchema, record));
    for (const listener of [...this.#listeners]) listener(bytes);
  }
}
