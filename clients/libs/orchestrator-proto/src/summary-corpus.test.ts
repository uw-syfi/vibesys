import {describe, expect, test} from 'bun:test';
import {readdirSync, readFileSync} from 'node:fs';
import {
  decodeValidated,
  decodeValidatedJson,
  encodeMessage,
  oneofIs,
  ReportScope,
  WatchRunsResponseSchema,
} from './index.js';

const corpus = new URL(
  '../../../../proto/vibesys/orchestrator/v1/conformance/summary/',
  import.meta.url,
);

/** Every kind of frame of the run stream, and of body of a message. */
const KINDS = {snapshot: true, change: true, search: true, summary: true} as const;
const MESSAGE_KINDS = {
  text: true,
  handoff: true,
  report: true,
  jobFinished: true,
  subagentSettled: true,
  brief: true,
  leadBrief: true,
  leadHandover: true,
  handoffReminder: true,
  leadIdleReminder: true,
  reportDue: true,
  catchUpRequest: true,
  runLimitReached: true,
} as const;

const read = (name: string) =>
  decodeValidatedJson(
    WatchRunsResponseSchema,
    JSON.parse(readFileSync(new URL(name, corpus), 'utf8')),
  );

describe('run summary conformance', () => {
  const files = readdirSync(corpus)
    .filter(name => name.endsWith('.json'))
    .sort();
  test('the whole corpus is registered', () => {
    expect(files).toEqual(['frame.json', 'snapshot.json']);
  });
  for (const name of files) {
    test(name, () => {
      const frame = read(name);
      expect(
        decodeValidated(WatchRunsResponseSchema, encodeMessage(WatchRunsResponseSchema, frame)),
      ).toEqual(frame);
    });
  }

  test('a summary frame carries the whole-run report and the counts the host worked out', () => {
    const {body} = read('frame.json');
    if (!oneofIs(body, 'summary', KINDS)) throw new Error('not a summary frame');
    const {report} = body.value;
    expect(oneofIs(report?.body, 'report', MESSAGE_KINDS) && report.body.value.scope).toBe(
      ReportScope.RUN,
    );
    expect(body.value.agentsWorking).toBe(2);
    expect(body.value.objectives.map(objective => objective.name)).toEqual([
      'latency',
      'tput',
      'memory',
    ]);
  });

  test('a snapshot says how many runs it left out', () => {
    const {body} = read('snapshot.json');
    if (!oneofIs(body, 'snapshot', KINDS)) throw new Error('not a snapshot');
    expect(body.value.runs).toHaveLength(2);
    expect(body.value.summaries.map(summary => summary.runId)).toEqual(['run-1', 'run-2']);
    expect([body.value.truncated, body.value.runsLeftOut]).toEqual([true, 7]);
  });
});
