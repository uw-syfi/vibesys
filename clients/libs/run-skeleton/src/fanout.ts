import {fromBinary, toBinary} from '@bufbuild/protobuf';
import {RecordSchema, type Record as StoreRecord} from '@vibesys/proto';
import type {RecordsListener} from './loader.js';
import {isRecordFact, RECORD_HISTORY_LIMIT} from './record-facts.js';

/** One reader supplies every fold, including subscribers mounted after the checkpoint arrived. */
export interface RecordsFanout {
  readonly publish: RecordsListener;
  subscribe(listener: RecordsListener): () => void;
}

export function recordsFanout(): RecordsFanout {
  const listeners = new Set<RecordsListener>();
  const facts = new Map<bigint, StoreRecord>();
  let recent: StoreRecord[] = [];
  return {
    publish: records => {
      const bySeq = new Map(recent.map(record => [record.seq, record]));
      for (const bytes of records) {
        const record = fromBinary(RecordSchema, bytes);
        if (isRecordFact(record)) facts.set(record.seq, record);
        bySeq.set(record.seq, record);
      }
      recent = ordered([...bySeq.values()]).slice(-RECORD_HISTORY_LIMIT);
      for (const listener of [...listeners]) listener(records);
    },
    subscribe: listener => {
      listeners.add(listener);
      const replay = new Map(facts);
      for (const record of recent) replay.set(record.seq, record);
      if (replay.size > 0)
        listener(ordered([...replay.values()]).map(record => toBinary(RecordSchema, record)));
      return () => {
        listeners.delete(listener);
      };
    },
  };
}

function ordered(records: StoreRecord[]): StoreRecord[] {
  return records.sort((a, b) => (a.seq < b.seq ? -1 : a.seq > b.seq ? 1 : 0));
}
