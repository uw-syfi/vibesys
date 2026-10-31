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
import {isCoreBody, type RowBodyCase} from './kinds.js';
import {recordMessage} from './message.js';

export {
  type CoreBodyCase,
  isCoreBody,
  RECORD_BODY_KIND,
  type RecordBodyCase,
  type RowBodyCase,
} from './kinds.js';
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
  readonly body: RowBodyCase;
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

/** The rows of records already decoded, such as the error notices a keyframe holds. */
export function rowsOfRecords(records: readonly StoreRecord[]): readonly RecordRow[] {
  return decodeRecordRows(records.map(record => toBinary(RecordSchema, record)));
}

/** The rows of the records a user reads. Core's bookkeeping records (`isCoreBody`) make no row. */
export function decodeRecordRows(bytes: readonly Uint8Array[]): readonly RecordRow[] {
  return bytes.flatMap(value => {
    const record = fromBinary(RecordSchema, value);
    const {body} = record;
    if (body.case === undefined) throw new Error(`record ${record.seq}: body required`);
    if (isCoreBody(body)) return [];
    return [
      {
        ...(body.case === 'blob' ? {attachment: body.value} : {}),
        seq: String(record.seq),
        epoch: record.epoch,
        body: body.case,
        event: body.case === 'legacyEvent' ? legacyType(body.value.json) : '',
        message: recordMessage(body),
        failure:
          (body.case === 'notice' && body.value.severity >= Severity.ERROR) ||
          (body.case === 'lifecycle' &&
            body.value.state.case === 'ended' &&
            body.value.state.value.failure !== undefined),
      },
    ];
  });
}
