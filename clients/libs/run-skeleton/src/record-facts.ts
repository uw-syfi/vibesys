import {create, toBinary} from '@bufbuild/protobuf';
import {type Keyframe, RecordSchema, type Record as StoreRecord} from '@vibesys/proto';

/** Every store body has an explicit recovery owner. Adding a body requires a decision here. */
export const RECORD_FACTS = {
  epochStarted: 'epochs',
  declarationAmended: 'epochs',
  span: 'spans',
  metric: 'metrics',
  lifecycle: 'lifecycles',
  stream: 'streams',
  notice: 'failures',
  intent: 'intents',
  blob: 'recordFacts',
  chat: 'recordFacts',
  legacyEvent: 'window',
} as const satisfies Record<NonNullable<StoreRecord['body']['case']>, string>;

/** The raw-record fold keeps 1000 rows; the shared reader must recover that whole bounded view. */
export const RECORD_HISTORY_LIMIT = 1000;

/** Facts consumed by folds outside the indexed skeleton, retained by the fanout for remounts. */
export function isRecordFact(record: StoreRecord): boolean {
  const {body} = record;
  switch (body.case) {
    case 'intent':
    case 'epochStarted':
    case 'blob':
    case 'chat':
      return true;
    case 'span':
      return body.value.span?.label?.links.some(link => link.rel === 'steer') ?? false;
    default:
      return false;
  }
}

/** Materialize the checkpoint's semantic records at their original writer positions. */
export function checkpointRecords(keyframe: Keyframe): Uint8Array[] {
  const records = [...keyframe.intents, ...keyframe.recordFacts, ...keyframe.failures];
  for (const version of keyframe.epochs) {
    if (!version.startsEpoch || !version.epoch) continue;
    records.push(
      create(RecordSchema, {
        seq: version.seq,
        epoch: version.epoch.epoch,
        at: version.startedAt,
        body: {case: 'epochStarted', value: version.epoch},
      }),
    );
  }
  return records
    .sort((a, b) => (a.seq < b.seq ? -1 : a.seq > b.seq ? 1 : 0))
    .map(record => toBinary(RecordSchema, record));
}
