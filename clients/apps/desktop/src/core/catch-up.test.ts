import {fromBinary} from '@bufbuild/protobuf';
import {describe, expect, it} from '@effect/vitest';
import {DispatchRequestSchema, RunStatus} from '@vibesys/orchestrator-proto';
import * as Effect from 'effect/Effect';
import * as Random from 'effect/Random';
import {type CatchUpPress, type CheckInRead, ms, rewind} from '../state/index.js';
import {
  busyFacts,
  FAKE_VIEWER,
  FakeDaemon,
  generateScenario,
  type HostHarness,
  hostHarness,
  methodPathOf,
  NO_VIEWER_REFUSAL,
} from '../testing/orchestrator/index.js';

const SEED = 20261107;

/** A test of the virtual clock whose jitter and ids are the same every run. */
const seeded = (name: string, body: () => Effect.Effect<void>) =>
  it.effect(name, () => body().pipe(Random.withSeed(SEED)));

const live = generateScenario({seed: 11, subagents: 2, live: true});
const ended = generateScenario({seed: 11, subagents: 2, live: false});
const RUN = live.runId;

/** Every Dispatch the daemon was asked for, as the id of its command. */
const dispatched = (h: HostHarness): string[] =>
  h.fake
    .calls()
    .filter(call => call.target.method === methodPathOf('Dispatch'))
    .map(call => fromBinary(DispatchRequestSchema, call.target.request).command?.id ?? '');

/** A host session over `daemon`, watching the run stream so what the host commits arrives. */
const watching = (daemon: FakeDaemon, options: Parameters<typeof hostHarness>[1] = {}) =>
  Effect.gen(function* () {
    const h = yield* hostHarness(daemon, {live: true, ...options});
    h.mount(h.session.runs.watch);
    yield* h.elapse(60, 5);
    return h;
  });

const standing = (h: HostHarness, runId = RUN) => h.read(h.session.catchUp.standing(runId));

/** This viewer's check-ins in the run, as the screen reads them, newest first. */
const mine = (h: HostHarness, runId = RUN): Effect.Effect<readonly CheckInRead[]> =>
  Effect.map(h.model, model => rewind(model.data, runId, FAKE_VIEWER.id));

const made = (press: CatchUpPress | undefined): string => {
  if (press?._tag !== 'Made') throw new Error(`the press was ${press?._tag ?? 'never made'}`);
  return press.checkInId;
};

describe('the press of Catch me up', () => {
  seeded('makes a check-in that is pending, then ready with the facts, then has the report', () =>
    Effect.scoped(
      Effect.gen(function* () {
        const daemon = new FakeDaemon(live);
        const h = yield* watching(daemon);
        expect(yield* standing(h)).toBeUndefined();

        yield* h.session.catchUp.press(RUN);
        yield* h.elapse(60, 5);
        const id = made(yield* standing(h));

        // The host answered at once; the check-in arrives on the run stream, with the lead asked.
        const [pending] = yield* mine(h);
        expect(pending?.checkIn.id).toBe(id);
        expect(pending?.facts).toEqual({kind: 'pending'});
        expect(pending?.summary).toEqual({kind: 'writing'});

        const facts = busyFacts(pending?.atMs ?? 0);
        daemon.settleCheckIn(id, facts);
        yield* h.elapse(60, 5);
        const [ready] = yield* mine(h);
        expect(ready?.facts).toEqual({kind: 'ready', facts});
        // The facts are in, and the lead is still writing.
        expect(ready?.summary).toEqual({kind: 'writing'});

        daemon.postReport(id, 'Best moved from 1.84x to 2.31x', 'Three ideas were rejected');
        yield* h.elapse(60, 5);
        const [written] = yield* mine(h);
        expect(written?.facts).toEqual({kind: 'ready', facts});
        expect(written?.summary).toMatchObject({
          kind: 'report',
          report: {headline: 'Best moved from 1.84x to 2.31x'},
        });
      }),
    ),
  );

  seeded('shows the reason the host gave when it could not work out the facts', () =>
    Effect.scoped(
      Effect.gen(function* () {
        const daemon = new FakeDaemon(live);
        const h = yield* watching(daemon);
        yield* h.session.catchUp.press(RUN);
        yield* h.elapse(60, 5);
        daemon.failCheckIn(made(yield* standing(h)), 'the job log is not answering');
        yield* h.elapse(60, 5);
        const [failed] = yield* mine(h);
        expect(failed?.facts).toEqual({kind: 'failed', reason: 'the job log is not answering'});
      }),
    ),
  );

  seeded('makes windows that follow one another, and a failed one moves nothing', () =>
    Effect.scoped(
      Effect.gen(function* () {
        const daemon = new FakeDaemon(live);
        const h = yield* watching(daemon);
        // A closed run stream is reopened, and a host whose streams keep closing is redialed after
        // a back-off, so each step lets a minute of virtual time pass.
        const press = (settle: (id: string) => void) =>
          Effect.gen(function* () {
            yield* h.session.catchUp.press(RUN);
            yield* h.elapse(60, 5);
            settle(made(yield* standing(h)));
            yield* h.elapse(60, 5);
          });
        yield* press(id => daemon.settleCheckIn(id, busyFacts(0)));
        yield* press(id => daemon.failCheckIn(id, 'the job log is away'));
        yield* press(id => daemon.settleCheckIn(id, busyFacts(0)));

        const [third, second, first] = yield* mine(h);
        // The first window starts with the run.
        expect(first?.sinceMs).toBe(ms((yield* h.model).data.runs[RUN]?.createdAt));
        // The second failed, so the third starts where the first ended: nothing is lost.
        expect(second?.facts.kind).toBe('failed');
        expect(third?.sinceMs).toBe(first?.atMs);
        expect(third?.atMs).toBeGreaterThanOrEqual(third?.sinceMs ?? 0);
      }),
    ),
  );

  seeded('answers a press made while the last check-in is pending with that check-in', () =>
    Effect.scoped(
      Effect.gen(function* () {
        const daemon = new FakeDaemon(live);
        const h = yield* watching(daemon);
        yield* h.session.catchUp.press(RUN);
        yield* h.elapse(60, 5);
        const first = made(yield* standing(h));
        yield* h.session.catchUp.press(RUN);
        yield* h.elapse(60, 5);
        expect(made(yield* standing(h))).toBe(first);
        expect(dispatched(h)).toHaveLength(2);
        expect(yield* mine(h)).toHaveLength(1);
      }),
    ),
  );

  seeded('asks the lead of a live run, and no one of a run that is over', () =>
    Effect.scoped(
      Effect.gen(function* () {
        const over = new FakeDaemon(ended);
        const h = yield* watching(over);
        yield* h.session.catchUp.press(ended.runId);
        yield* h.elapse(60, 5);
        const [checkIn] = yield* mine(h, ended.runId);
        expect(checkIn?.checkIn.askMessageId).toBe('');
        expect(checkIn?.summary.kind).toBe('reports');
        const run = (yield* h.model).data.runs[ended.runId];
        expect(run?.status).not.toBe(RunStatus.RUNNING);
        // The window ends where the run did, however late the press.
        expect(checkIn?.atMs).toBeGreaterThanOrEqual(checkIn?.sinceMs ?? 0);
      }),
    ),
  );

  seeded('says the run is not on the host as a refusal, and makes no check-in', () =>
    Effect.scoped(
      Effect.gen(function* () {
        const h = yield* watching(new FakeDaemon(live));
        yield* h.session.catchUp.press('no-such-run');
        yield* h.elapse(60, 5);
        expect(yield* standing(h, 'no-such-run')).toEqual({
          _tag: 'Refused',
          reason: 'run-not-found',
        });
        expect(yield* mine(h, 'no-such-run')).toHaveLength(0);
      }),
    ),
  );

  seeded('says a call with no viewer is refused, naming the field the host named', () =>
    Effect.scoped(
      Effect.gen(function* () {
        const h = yield* watching(new FakeDaemon(live, undefined, null));
        yield* h.session.catchUp.press(RUN);
        yield* h.elapse(60, 5);
        expect(NO_VIEWER_REFUSAL).toContain('vibesys-viewer-id');
        expect(yield* standing(h)).toEqual({_tag: 'Refused', reason: 'no-viewer'});
        expect(yield* mine(h)).toHaveLength(0);
      }),
    ),
  );

  seeded('presses again under the same id when it could not tell how the press went', () =>
    Effect.scoped(
      Effect.gen(function* () {
        const daemon = new FakeDaemon(live);
        const h = yield* watching(daemon, {dispatch: nth => (nth === 0 ? 'lose-request' : 'ok')});
        yield* h.session.catchUp.press(RUN);
        yield* h.elapse(60, 5);
        expect(yield* standing(h)).toMatchObject({_tag: 'Unconfirmed'});
        expect(yield* mine(h)).toHaveLength(0);

        yield* h.session.catchUp.press(RUN);
        yield* h.elapse(60, 5);
        made(yield* standing(h));
        const ids = dispatched(h);
        expect(ids).toHaveLength(2);
        expect(ids[1]).toBe(ids[0]);
        expect(yield* mine(h)).toHaveLength(1);
      }),
    ),
  );

  seeded('makes one check-in when the reply to the press was lost and the press is retried', () =>
    Effect.scoped(
      Effect.gen(function* () {
        const daemon = new FakeDaemon(live);
        const h = yield* hostHarness(daemon, {
          dispatch: nth => (nth === 0 ? 'lose-reply' : 'ok'),
        });
        yield* h.session.catchUp.press(RUN);
        yield* h.elapse(60, 5);
        expect(yield* standing(h)).toMatchObject({_tag: 'Unconfirmed'});
        yield* h.session.catchUp.press(RUN);
        yield* h.elapse(60, 5);
        const id = made(yield* standing(h));
        // The host held the receipt of the first send: the same check-in, not a second window.
        h.mount(h.session.runs.watch);
        yield* h.elapse(60, 5);
        expect((yield* mine(h)).map(each => each.checkIn.id)).toEqual([id]);
      }),
    ),
  );

  seeded('sends a press once while it is still on its way', () =>
    Effect.scoped(
      Effect.gen(function* () {
        // No run stream is mounted, so only the reply to the press can settle it.
        const h = yield* hostHarness(new FakeDaemon(live), {
          wrap: base => ({
            respond: (target, nth) => {
              const steps = base.respond(target, nth);
              return target.method === methodPathOf('Dispatch')
                ? [{sleep: '3 seconds' as const}, ...steps]
                : steps;
            },
          }),
        });
        yield* h.session.catchUp.press(RUN);
        yield* h.session.catchUp.press(RUN);
        yield* h.session.catchUp.press(RUN);
        expect(yield* standing(h)).toEqual({_tag: 'Pressing'});
        yield* h.elapse(10);
        made(yield* standing(h));
        expect(dispatched(h)).toHaveLength(1);
      }),
    ),
  );

  seeded('keeps the standing of each run apart', () =>
    Effect.scoped(
      Effect.gen(function* () {
        const h = yield* watching(new FakeDaemon(live));
        yield* h.session.catchUp.press(RUN);
        yield* h.session.catchUp.press('other-run');
        yield* h.elapse(60, 5);
        expect((yield* standing(h, RUN))?._tag).toBe('Made');
        expect((yield* standing(h, 'other-run'))?._tag).toBe('Refused');
      }),
    ),
  );
});
