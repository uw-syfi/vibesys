/** Which store record bodies the raw record view shows. The one place that decides it. */
import type {Record as StoreRecord} from '@vibesys/proto';

export type RecordBodyCase = NonNullable<StoreRecord['body']['case']>;

/**
 * `row`: the record is something a user reads in the run history. `core`: runtime bookkeeping of
 * core's own recovery (docs/contributing/run-recovery.md); no screen shows it. A body added to the
 * store schema fails type-checking here until someone decides which it is.
 */
export const RECORD_BODY_KIND = {
  epochStarted: 'row',
  declarationAmended: 'row',
  span: 'row',
  metric: 'row',
  lifecycle: 'row',
  stream: 'row',
  notice: 'row',
  intent: 'row',
  blob: 'row',
  chat: 'row',
  legacyEvent: 'row',
  coreInput: 'core',
  stepStarted: 'core',
  stepFinished: 'core',
  attemptInterrupted: 'core',
  runEnded: 'core',
  runStarted: 'core',
  coreSnapshot: 'core',
  strategyAdvanced: 'core',
} as const satisfies Record<RecordBodyCase, 'row' | 'core'>;

/** The bodies core writes for its own bookkeeping. */
export type CoreBodyCase = {
  [K in RecordBodyCase]: (typeof RECORD_BODY_KIND)[K] extends 'core' ? K : never;
}[RecordBodyCase];

/** The bodies a row can be made of. */
export type RowBodyCase = Exclude<RecordBodyCase, CoreBodyCase>;

export type CoreBody = Extract<StoreRecord['body'], {case: CoreBodyCase}>;
export type RowBody = Exclude<StoreRecord['body'], CoreBody | {case: undefined}>;

export function isCoreBody(body: StoreRecord['body']): body is CoreBody {
  return body.case !== undefined && RECORD_BODY_KIND[body.case] === 'core';
}
