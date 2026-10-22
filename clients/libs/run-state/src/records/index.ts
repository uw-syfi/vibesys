/** The bounded raw record projection, independent of connection control state. */
import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import {
  HostService,
  RecordSchema,
  Severity,
  type Record as StoreRecord,
  TailRequestSchema,
  TailResponseSchema,
} from '@vibesys/proto';
import {recordMessage} from './message.js';
export const TAIL_RECORDS_METHOD = `/${HostService.typeName}/${HostService.method.tail.name}`;
export function tailRecordsRequest(runId: string, fromSeq = 1n): Uint8Array {
  return toBinary(TailRequestSchema, create(TailRequestSchema, {runId, fromSeq}));
}
export function tailRecordRows(bytes: Uint8Array): readonly RecordRow[] {
  return decodeRecordRows(fromBinary(TailResponseSchema, bytes).records);
}

export interface RecordRow {
  readonly seq: string;
  readonly epoch: number;
  readonly body: NonNullable<StoreRecord['body']['case']>;
  readonly event: string;
  readonly message: string;
  readonly failure: boolean;
}

export function recordRows(
  rows: readonly RecordRow[],
  next: readonly RecordRow[],
): readonly RecordRow[] {
  return [...rows, ...next].slice(-1000);
}

export function decodeRecordRows(bytes: readonly Uint8Array[]): readonly RecordRow[] {
  return bytes.map(value => {
    const record = fromBinary(RecordSchema, value);
    if (record.body.case === undefined) throw new Error(`record ${record.seq}: body required`);
    let event = '';
    if (record.body.case === 'legacyEvent') {
      const value: unknown = JSON.parse(record.body.value.json);
      if (
        typeof value === 'object' &&
        value !== null &&
        'type' in value &&
        typeof value.type === 'string'
      )
        event = value.type;
    }
    return {
      seq: String(record.seq),
      epoch: record.epoch,
      body: record.body.case,
      event,
      message: recordMessage(record.body),
      failure:
        (record.body.case === 'notice' && record.body.value.severity >= Severity.ERROR) ||
        (record.body.case === 'lifecycle' &&
          record.body.value.state.case === 'ended' &&
          record.body.value.state.value.failure !== undefined),
    };
  });
}
