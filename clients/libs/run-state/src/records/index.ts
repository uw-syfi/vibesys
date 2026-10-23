/** The bounded raw record projection, independent of connection control state. */
import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import {
  type BlobAttached,
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
  readonly attachment?: BlobAttached;
}

/** The latest 1000 rows by sequence; a row read twice (a Tail window over the prefix) counts once. */
export function recordRows(
  rows: readonly RecordRow[],
  next: readonly RecordRow[],
): readonly RecordRow[] {
  const bySeq = new Map<string, RecordRow>();
  for (const row of [...rows, ...next]) bySeq.set(row.seq, row);
  return [...bySeq.values()].sort((a, b) => (BigInt(a.seq) < BigInt(b.seq) ? -1 : 1)).slice(-1000);
}

function legacyType(json: string): string {
  let value: unknown;
  try {
    value = JSON.parse(json);
  } catch (error) {
    if (error instanceof SyntaxError) return '';
    throw error;
  }
  return typeof value === 'object' &&
    value !== null &&
    'type' in value &&
    typeof value.type === 'string'
    ? value.type
    : '';
}

export function decodeRecordRows(bytes: readonly Uint8Array[]): readonly RecordRow[] {
  return bytes.map(value => {
    const record = fromBinary(RecordSchema, value);
    if (record.body.case === undefined) throw new Error(`record ${record.seq}: body required`);
    return {
      ...(record.body.case === 'blob' ? {attachment: record.body.value} : {}),
      seq: String(record.seq),
      epoch: record.epoch,
      body: record.body.case,
      event: record.body.case === 'legacyEvent' ? legacyType(record.body.value.json) : '',
      message: recordMessage(record.body),
      failure:
        (record.body.case === 'notice' && record.body.value.severity >= Severity.ERROR) ||
        (record.body.case === 'lifecycle' &&
          record.body.value.state.case === 'ended' &&
          record.body.value.state.value.failure !== undefined),
    };
  });
}
