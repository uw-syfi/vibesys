import {expect, test} from 'bun:test';
import {create, type MessageInitShape} from '@bufbuild/protobuf';
import {
  LifecycleSchema,
  NoticeCode,
  NoticeSchema,
  Outcome,
  RecordSchema,
  RunFailureKind,
  RunLiveness,
  Severity,
  type Record as StoreRecord,
} from '@vibesys/proto';
import {storeRunFixture} from '@vibesys/run-fixtures';
import {foldSkeleton, seedSkeleton} from '@vibesys/run-skeleton';
import {rowsOfRecords} from '@vibesys/run-state/records';
import {
  commandErrorReport,
  diagnosticErrorReport,
  endedBannerWords,
  endedErrorReport,
  headerView,
  projectSkeleton,
  type RunPageState,
  runPageInitial,
  shownDiagnostic,
  skeletonScreenState,
} from './index.js';

const SEED = {
  identity: {host: 'local' as const, runId: 'run-a'},
  title: {project: 'queue-rs', task: null},
  hostLabel: 'Local',
  unavailable: {},
};

/** The `short` fixture's records, then whatever the run writes next. */
function screen(
  next: readonly StoreRecord['body'][],
  hint?: {readonly epoch: number; readonly value: RunLiveness},
  tailed = false,
): RunPageState {
  const records: StoreRecord[] = [...storeRunFixture('short').records];
  for (const body of next)
    records.push(
      create(RecordSchema, {
        seq: BigInt(records.length + 1),
        epoch: 1,
        at: {seconds: 1700001000n},
        body,
      }),
    );
  const skeleton = foldSkeleton(seedSkeleton(), records);
  const whole = projectSkeleton(skeleton, skeleton.throughSeq ?? 0n);
  // A tailed page also holds the latest records as rows, which is where a diagnostic comes from.
  const ui = tailed
    ? {...runPageInitial(SEED), recordLog: rowsOfRecords(records.slice(-next.length))}
    : runPageInitial(SEED);
  return skeletonScreenState(SEED, skeleton, whole, ui, hint);
}

const lifecycle = (init: MessageInitShape<typeof LifecycleSchema>): StoreRecord['body'] => ({
  case: 'lifecycle',
  value: create(LifecycleSchema, init),
});

const FAILED = lifecycle({
  state: {
    case: 'ended',
    value: {
      outcome: Outcome.FAILED,
      failure: {
        kind: RunFailureKind.ENVIRONMENT_UNAVAILABLE,
        reason: 'Git refused the workspace',
      },
    },
  },
});

test('the ended banner asks in its own words: where names the header word, the message its reason', () => {
  const failed = screen([FAILED]);
  const report = endedErrorReport(failed);
  const words = endedBannerWords(failed);
  expect(report?.where).toBe(`Run ended: ${words?.title}`);
  // The question puts `where` in parentheses; it holds none of its own.
  expect(report?.where).not.toMatch(/[()]/);
  expect(report?.where).toContain(headerView(failed).status.label);
  expect(report?.message).toContain('Git refused the workspace');
  expect(report?.message).toBe(words?.body);
  expect(report?.detail).toBeNull();

  const interrupted = screen([lifecycle({state: {case: 'running', value: {}}})], {
    epoch: 1,
    value: RunLiveness.INTERRUPTED,
  });
  expect(headerView(interrupted).status.label).toBe('Interrupted');
  expect(endedErrorReport(interrupted)?.where).toContain('Interrupted');
  expect(endedErrorReport(interrupted)?.message).not.toBe('');

  const finished = screen([
    lifecycle({state: {case: 'ended', value: {outcome: Outcome.FINISHED}}}),
  ]);
  expect(endedErrorReport(finished)).toBeNull();
  expect(endedErrorReport(screen([lifecycle({state: {case: 'running', value: {}}})]))).toBeNull();
});

test("the Run diagnostic asks with the record's message and names the record", () => {
  const noticed = screen([
    lifecycle({state: {case: 'running', value: {}}}),
    {
      case: 'notice',
      value: create(NoticeSchema, {
        severity: Severity.ERROR,
        code: NoticeCode.INPUT_MEASUREMENT_FAILED,
        message: 'Measurement unavailable',
      }),
    },
  ]);
  const row = noticed.recordDiagnostic;
  if (row === undefined) throw new Error('an error notice is a diagnostic');
  expect(diagnosticErrorReport(noticed)).toEqual({
    where: 'Run diagnostic',
    message: 'Measurement unavailable',
    detail: `Record ${row.seq}: ${row.event}`,
  });
  expect(diagnosticErrorReport(screen([]))).toBeNull();
});

test('a diagnostic that is the run ending is asked about once, from the ended banner', () => {
  const failed = screen([FAILED], undefined, true);
  expect(failed.recordDiagnostic?.body).toBe('lifecycle');
  expect(endedErrorReport(failed)).not.toBeNull();
  expect(diagnosticErrorReport(failed)).toBeNull();
});

test('a diagnostic is shown unless it repeats the reason the ended banner gives', () => {
  const failed = screen([FAILED], undefined, true);
  expect(failed.recordDiagnostic?.message).toContain(endedBannerWords(failed)?.body ?? '?');
  expect(shownDiagnostic(failed)).toBeNull();
  const noticed = screen(
    [
      lifecycle({state: {case: 'running', value: {}}}),
      {
        case: 'notice',
        value: create(NoticeSchema, {
          severity: Severity.ERROR,
          code: NoticeCode.INPUT_MEASUREMENT_FAILED,
          message: 'Measurement unavailable',
        }),
      },
    ],
    undefined,
    true,
  );
  expect(noticed.recordDiagnostic).toBeDefined();
  expect(shownDiagnostic(noticed)).toBe(noticed.recordDiagnostic ?? null);
  expect(shownDiagnostic(screen([]))).toBeNull();
});

test('a command the run did not take asks with its notice', () => {
  const state = screen([]);
  expect(commandErrorReport(state)).toBeNull();
  expect(commandErrorReport({...state, notice: 'The run is not running'})).toEqual({
    where: 'Run command',
    message: 'The run is not running',
    detail: null,
  });
});
