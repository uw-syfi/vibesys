/** The bounded raw record projection, independent of connection control state. */
import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import {HostService, RecordSchema, TailRequestSchema, TailResponseSchema} from '@vibesys/proto';
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
  readonly body: string;
  readonly event: string;
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
      body: record.body.case ?? 'unknown',
      event,
    };
  });
}
