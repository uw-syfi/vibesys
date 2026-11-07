import {create, toBinary} from '@bufbuild/protobuf';
import {describe, expect, it} from '@effect/vitest';
import {SnapshotDetail, WatchRunsRequestSchema} from '@vibesys/orchestrator-proto';
import * as Effect from 'effect/Effect';
import {
  FakeDaemon,
  generateScenario,
  type HostHarness,
  hostHarness,
  methodPathOf,
} from '../testing/orchestrator/index.js';
import type {Recording} from '../testing/transport/index.js';
import {RpcStatus} from './errors.js';

const scenario = generateScenario({seed: 5, subagents: 4, live: false});
const RUN = scenario.runId;

const callsTo = (h: HostHarness, method: string) =>
  h.fake.calls().filter(call => call.target.method === methodPathOf(method));

const messagesOf = (h: HostHarness) =>
  h.model.pipe(
    Effect.map(model =>
      Object.values(model.data.messages)
        .filter(message => message.runId === RUN)
        .map(message => message.id)
        .sort(),
    ),
  );

const hostMessages = (daemon: FakeDaemon): string[] =>
  daemon
    .snapshot()
    .messages.filter(message => message.runId === RUN)
    .map(message => message.id)
    .sort();

/**
 * The host answers the second opening of the run stream, a minute after it is made, with a
 * snapshot, as it does a reader that fell behind. Every other opening resumes from the sequence the
 * reader holds.
 */
const resyncs = (base: Recording): Recording => ({
  respond: (target, nth) =>
    target.method === methodPathOf('WatchRuns') && nth === 1
      ? [
          {sleep: '60 seconds'},
          ...base.respond(
            {
              ...target,
              request: toBinary(
                WatchRunsRequestSchema,
                create(WatchRunsRequestSchema, {detail: SnapshotDetail.SUMMARY}),
              ),
            },
            nth,
          ),
        ]
      : base.respond(target, nth),
});

describe('a run history read on request', () => {
  it.effect(
    'is not in the run stream, and is read page by page while a screen of the run is open',
    () =>
      Effect.gen(function* () {
        const daemon = new FakeDaemon(scenario);
        const want = hostMessages(daemon);
        expect(want.length).toBeGreaterThan(6);
        const h = yield* hostHarness(daemon, {historyPageMessages: 3});
        h.mount(h.session.runs.watch);
        yield* h.elapse(5);
        // The stream's snapshot lists the run and its summary, and holds none of its history.
        const listed = (yield* h.model).data;
        expect(Object.keys(listed.runs)).toEqual([RUN]);
        expect(listed.summaries[RUN]?.runId).toBe(RUN);
        expect([Object.keys(listed.agents), Object.keys(listed.messages)]).toEqual([[], []]);
        expect(callsTo(h, 'GetRunHistory')).toHaveLength(0);

        h.mount(h.session.runHistory.watch(RUN));
        yield* h.elapse(5);
        const read = (yield* h.model).data;
        expect(yield* messagesOf(h)).toEqual(want);
        expect(Object.keys(read.agents).length).toBeGreaterThan(0);
        expect(read.histories[RUN]).toMatchObject({complete: true});
        // Every page was asked for once, and the last was the one that said there were no more.
        expect(callsTo(h, 'GetRunHistory')).toHaveLength(Math.ceil(want.length / 3));
        expect(yield* h.read(h.session.runHistory.status(RUN))).toEqual({_tag: 'Idle'});
      }),
  );

  it.effect(
    'is read again after a new snapshot forgot it, and not when the stream only resumes',
    () =>
      Effect.gen(function* () {
        const daemon = new FakeDaemon(scenario);
        const h = yield* hostHarness(daemon, {closeStreams: true, wrap: resyncs});
        h.mount(h.session.runs.watch);
        yield* h.elapse(5);
        h.mount(h.session.runHistory.watch(RUN));
        yield* h.elapse(5);
        expect(callsTo(h, 'GetRunHistory')).toHaveLength(1);
        expect(yield* messagesOf(h)).toEqual(hostMessages(daemon));
        // Openings that only resume forget nothing, so nothing is read again.
        yield* h.elapse(20, 5);
        expect(callsTo(h, 'GetRunHistory')).toHaveLength(1);
        // The snapshot forgets the history, and the screen reads it once more.
        yield* h.elapse(60, 5);
        expect(callsTo(h, 'GetRunHistory')).toHaveLength(2);
        expect(yield* messagesOf(h)).toEqual(hostMessages(daemon));
        expect((yield* h.model).data.histories[RUN]?.complete).toBe(true);
      }),
  );

  it.effect('says it could not be read, and reads again when the person retries', () =>
    Effect.gen(function* () {
      const daemon = new FakeDaemon(scenario);
      const h = yield* hostHarness(daemon, {
        wrap: base => ({
          respond: (target, nth) =>
            target.method === methodPathOf('GetRunHistory') && nth === 0
              ? [{end: {message: 'no answer', status: RpcStatus.UNAVAILABLE}}]
              : base.respond(target, nth),
        }),
      });
      h.mount(h.session.runs.watch);
      h.mount(h.session.runHistory.watch(RUN));
      yield* h.elapse(5);
      const stuck = yield* h.read(h.session.runHistory.status(RUN));
      expect(stuck._tag).toBe('Failed');
      expect((yield* h.model).data.histories[RUN]).toBeUndefined();
      yield* h.session.runHistory.retry(RUN);
      yield* h.elapse(5);
      expect(yield* messagesOf(h)).toEqual(hostMessages(daemon));
      expect(yield* h.read(h.session.runHistory.status(RUN))).toEqual({_tag: 'Idle'});
    }),
  );
});
