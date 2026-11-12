/**
 * What a host that answers but cannot run a run does to the app. Two hosts met on a live check: one
 * whose orchestrator refused its state directory (a network disk), which the Runs list showed as "Not
 * read yet" with a Retry that changed nothing, and one that served but whose job tool was older than
 * the task, which failed in the middle of a run after agents had spent tokens. Both are what the host
 * says in `Identify`; the app folds that into one typed cause, shows it, and does not retry it.
 */
import {create, type MessageInitShape} from '@bufbuild/protobuf';
import {timestampFromMs} from '@bufbuild/protobuf/wkt';
import {describe, expect, it} from '@effect/vitest';
import {
  HostPart,
  type IdentifyResponse,
  IdentifyResponseSchema,
  PartProblemReason,
  PartReadinessSchema,
  ReleaseRelation,
  SendMessageSchema,
  StartRunSchema,
  StateRefusal,
  UnservedReason,
} from '@vibesys/orchestrator-proto';
import * as Effect from 'effect/Effect';
import * as Schema from 'effect/Schema';
import {
  type CommandProgress,
  partRefusalOf,
  REFUSAL_CAUSES,
  type RefusalCause,
  readinessOf,
  refusalCauseOf,
} from '../state/index.js';
import {
  FakeDaemon,
  generateScenario,
  hostHarness,
  methodPathOf,
} from '../testing/orchestrator/index.js';
import type {Intent} from './commands.js';
import {RpcStatus} from './errors.js';

const daemon = () => new FakeDaemon(generateScenario({seed: 11, subagents: 1, live: true}));

const PARTS = [HostPart.SESSION_HOLDER, HostPart.SKILLS, HostPart.JOB_TOOL] as const;
const REASONS = [
  PartProblemReason.NO_RELEASE,
  PartProblemReason.RELEASE_INCOMPLETE,
  PartProblemReason.OPERATOR_CHOICE_REFUSED,
  PartProblemReason.TOO_OLD,
] as const;

/** What a part of a host says: ready, or a problem with `reason`. */
const part = (which: HostPart, reason: PartProblemReason | null, detail = 'in the host’s words') =>
  create(PartReadinessSchema, {
    part: which,
    state:
      reason === null
        ? {case: 'ready', value: {origin: 1}}
        : {case: 'problem', value: {reason, detail}},
  });

/** What a host says of itself: it serves or not, and each part as it is. */
const identify = (
  orchestrator: NonNullable<MessageInitShape<typeof IdentifyResponseSchema>['orchestrator']>,
  parts: IdentifyResponse['parts'] = [],
): IdentifyResponse =>
  create(IdentifyResponseSchema, {
    release: '1.0.0',
    relation: ReleaseRelation.SAME,
    startedAt: timestampFromMs(0),
    parts,
    orchestrator,
  });

const serving = {case: 'serving', value: {}} as const;
const unserved = (reason: UnservedReason, stateRefusal = StateRefusal.UNSPECIFIED) =>
  ({case: 'unserved', value: {reason, stateRefusal, detail: 'words'}}) as const;

const start: Intent = {
  case: 'startRun',
  value: create(StartRunSchema, {
    repository: '/repo',
    task: 'demo',
    fanoutLimit: 1,
    lead: {harness: 1},
  }),
};

const dispatches = (h: {fake: {calls: () => readonly {target: {method: string}}[]}}) =>
  h.fake.calls().filter(call => call.target.method === methodPathOf('Dispatch')).length;

describe('what a host says stands in the way of a run', () => {
  const status = Schema.Union([Schema.Null, Schema.Literals(REASONS)]);
  const statuses = Schema.Tuple([status, status, status]);

  it.effect.prop(
    'is, for every combination of parts, the first problem as a typed cause, and nothing when all are ready',
    {statuses},
    ({statuses}) =>
      Effect.sync(() => {
        const parts = PARTS.map((which, at) => part(which, statuses[at] ?? null));
        const refusal = partRefusalOf(parts);
        const firstProblem = statuses.findIndex(status => status !== null);
        if (firstProblem < 0) {
          expect(refusal).toBeUndefined();
          return;
        }
        const reason = statuses[firstProblem];
        expect(refusal).toBeDefined();
        expect(REFUSAL_CAUSES).toContain(refusal?.cause);
        // The cause says which part, and whether it is too old or missing.
        const word = ['session-holder', 'skills', 'job-tool'][firstProblem];
        expect(refusal?.cause).toBe(
          `${word}-${reason === PartProblemReason.TOO_OLD ? 'too-old' : 'missing'}`,
        );
        expect(refusal?.detail).toBe('in the host’s words');
      }),
  );

  it('puts a host that does not serve before a part that is missing, and a serving one after', () => {
    const missing = [part(HostPart.JOB_TOOL, PartProblemReason.TOO_OLD)];
    expect(
      readinessOf(
        identify(unserved(UnservedReason.STATE_REFUSED, StateRefusal.NETWORK_FILESYSTEM), missing),
      )?.cause,
    ).toBe('state-network-filesystem');
    expect(readinessOf(identify(serving, missing))?.cause).toBe('job-tool-too-old');
    expect(readinessOf(identify(serving, [part(HostPart.JOB_TOOL, null)]))).toBeUndefined();
    // A host held by another process is waited out, not refused.
    expect(readinessOf(identify(unserved(UnservedReason.HELD_ELSEWHERE)))).toBeUndefined();
  });

  it.effect('is learned when the link is Ready, beside the runs, and the runs are still read', () =>
    Effect.scoped(
      Effect.gen(function* () {
        const h = yield* hostHarness(daemon(), {
          identify: () => identify(serving, [part(HostPart.JOB_TOOL, PartProblemReason.TOO_OLD)]),
        });
        yield* h.session.connection.ready();
        yield* h.elapse(5);
        const state = yield* h.read(h.session.connection.state);
        expect(state).toMatchObject({_tag: 'Ready', blocked: {cause: 'job-tool-too-old'}});
        expect(refusalCauseOf(state)).toBe('job-tool-too-old');
      }),
    ),
  );

  it.effect('refuses a start in the host’s typed words before any agent is launched', () =>
    Effect.scoped(
      Effect.gen(function* () {
        const h = yield* hostHarness(daemon(), {
          identify: () =>
            identify(serving, [part(HostPart.JOB_TOOL, PartProblemReason.TOO_OLD, 'old build')]),
        });
        const id = yield* h.session.commands.submit(start);
        yield* h.elapse(60, 5);
        const progress: CommandProgress | undefined = yield* h.read(
          h.session.commands.progress(id),
        );
        expect(progress).toMatchObject({
          _tag: 'Failed',
          fault: {
            kind: 'Refused',
            cause: 'job-tool-too-old',
            detail: 'old build',
            retryable: false,
          },
        });
        expect(dispatches(h)).toBe(0);
        // Telling a running agent something is not a start, and goes through.
        const said = yield* h.session.commands.submit({
          case: 'sendMessage',
          value: create(SendMessageSchema, {agentId: 'x', text: 'hello'}),
        });
        yield* h.elapse(60, 5);
        expect(yield* h.read(h.session.commands.progress(said))).not.toMatchObject({
          fault: {kind: 'Refused'},
        });
        expect(dispatches(h)).toBe(1);
      }),
    ),
  );

  it.effect('sends a start to a host whose parts are all ready', () =>
    Effect.scoped(
      Effect.gen(function* () {
        const h = yield* hostHarness(daemon(), {
          identify: () =>
            identify(
              serving,
              PARTS.map(which => part(which, null)),
            ),
        });
        yield* h.session.commands.submit(start);
        yield* h.elapse(60, 5);
        expect(dispatches(h)).toBe(1);
      }),
    ),
  );

  it.effect(
    'is asked again at the next start, so a repaired host is sent it and its chip clears',
    () =>
      Effect.scoped(
        Effect.gen(function* () {
          let repaired = false;
          const h = yield* hostHarness(daemon(), {
            identify: () =>
              identify(serving, [
                part(HostPart.JOB_TOOL, repaired ? null : PartProblemReason.TOO_OLD),
              ]),
          });
          yield* h.session.connection.ready();
          yield* h.elapse(5);
          expect(refusalCauseOf(yield* h.read(h.session.connection.state))).toBe(
            'job-tool-too-old',
          );
          yield* h.session.commands.submit(start);
          yield* h.elapse(60, 5);
          expect(dispatches(h)).toBe(0);
          repaired = true;
          yield* h.session.commands.submit(start);
          yield* h.elapse(60, 5);
          expect(dispatches(h)).toBe(1);
          expect(refusalCauseOf(yield* h.read(h.session.connection.state))).toBeNull();
        }),
      ),
  );
});

describe('a host whose orchestrator is unserved, read through a handshake it answers UNAVAILABLE', () => {
  const causes = {
    [StateRefusal.NETWORK_FILESYSTEM]: 'state-network-filesystem',
    [StateRefusal.UNKNOWN_FILESYSTEM]: 'state-unknown-filesystem',
    [StateRefusal.UNSPECIFIED]: 'state-refused',
  } as const satisfies Record<StateRefusal, RefusalCause>;

  it.effect('is Unusable with the cause, asked once and never redialed, for each place', () =>
    Effect.scoped(
      Effect.gen(function* () {
        for (const place of [
          StateRefusal.NETWORK_FILESYSTEM,
          StateRefusal.UNKNOWN_FILESYSTEM,
          StateRefusal.UNSPECIFIED,
        ]) {
          const h = yield* hostHarness(daemon(), {
            helloFails: () => RpcStatus.UNAVAILABLE,
            identify: () => identify(unserved(UnservedReason.STATE_REFUSED, place)),
          });
          const failure = yield* Effect.flip(h.session.connection.ready());
          expect(failure.state).toMatchObject({
            _tag: 'Unusable',
            fault: {kind: 'Refused', cause: causes[place], retryable: false},
          });
          yield* h.elapse(600, 60);
          const hellos = h.fake
            .calls()
            .filter(call => call.target.method === methodPathOf('Hello'));
          expect(hellos).toHaveLength(1);
          expect(refusalCauseOf(yield* h.read(h.session.connection.state))).toBe(causes[place]);
        }
      }),
    ),
  );

  it.effect('is redialed when the host is only held by another process', () =>
    Effect.scoped(
      Effect.gen(function* () {
        const h = yield* hostHarness(daemon(), {
          helloFails: () => RpcStatus.UNAVAILABLE,
          identify: () => identify(unserved(UnservedReason.HELD_ELSEWHERE)),
        });
        yield* Effect.forkChild(h.session.connection.ready());
        yield* h.elapse(120, 10);
        const hellos = h.fake.calls().filter(call => call.target.method === methodPathOf('Hello'));
        expect(hellos.length).toBeGreaterThan(1);
        expect(refusalCauseOf(yield* h.read(h.session.connection.state))).toBeNull();
      }),
    ),
  );
});
