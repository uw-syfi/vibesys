import {expect, test} from 'bun:test';
import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import type {HostId} from '@vibesys/hosts';
import {
  IdeaVerdict,
  ListRunDigestsResponseSchema,
  ReadRequestSchema,
  ReadResponseSchema,
  RunAttention,
  RunClosing,
  RunEndOutcome,
} from '@vibesys/proto';
import fc from 'fast-check';
import {digestEntries, registryRunReads} from './registry-run-reads.js';

const HOST: HostId = {kind: 'local'};

/** The gateway's read port, answering with what the test says each read returns. */
function answering(reply: (method: ReturnType<typeof requestOf>) => Uint8Array) {
  const requests: ReturnType<typeof requestOf>[] = [];
  const reads = registryRunReads({
    checkout: async () => '/checkout',
    unary: async (_host, bytes) => {
      const request = requestOf(bytes);
      requests.push(request);
      return reply(request);
    },
  });
  return {reads, requests};
}

function requestOf(bytes: Uint8Array) {
  return fromBinary(ReadRequestSchema, bytes).method;
}

const response = (result: Parameters<typeof create<typeof ReadResponseSchema>>[1]) =>
  toBinary(ReadResponseSchema, create(ReadResponseSchema, result));

const created = {seconds: 1_790_000_000n, nanos: 500_000_000};

test('digests ask for the named runs with their tiles and restate each digest in the app terms', async () => {
  const {reads, requests} = answering(method => {
    if (method.case !== 'listRunDigests') throw new Error('unexpected read');
    return response({
      result: {
        case: 'listRunDigests',
        value: {
          requestId: method.value.requestId,
          digests: [
            {
              runId: 'run-a',
              createdAt: created,
              result: {
                case: 'digest',
                value: {
                  ideas: 3,
                  adoptedRatio: 1.5,
                  durationS: 10,
                  ending: {outcome: RunEndOutcome.KEPT_INPUT, summary: ' ', uncompared: true},
                  closing: RunClosing.FINISHED,
                  dashboard: {
                    points: [
                      {ratio: 1.2, verdict: IdeaVerdict.KEPT, at: created},
                      {verdict: IdeaVerdict.RUNNING},
                    ],
                    pointsOmitted: 4,
                    failedStreak: 1,
                    attention: RunAttention.REPEATED_FAILURE,
                    best: {value: 287.4, metric: 'output_tokens_per_s'},
                  },
                },
              },
            },
            {runId: 'run-b', createdAt: created, result: {case: 'unreadable', value: 'unreadable'}},
          ],
        },
      },
    });
  });

  const answer = await reads.digests(
    HOST,
    '/project',
    ['run-a', 'run-b'],
    new AbortController().signal,
  );

  const [request] = requests;
  expect(request).toMatchObject({
    case: 'listRunDigests',
    value: {project: '/project', runIds: ['run-a', 'run-b'], withDashboard: true},
  });
  if (answer.kind !== 'digests') throw new Error('digests required');
  expect(answer.runs.get('run-a')).toEqual({
    kind: 'read',
    createdAt: 1_790_000_000_500,
    ideas: 3,
    adoptedRatio: 1.5,
    bestSoFarRatio: null,
    durationSeconds: 10,
    outcome: 'kept-input',
    uncompared: true,
    closing: 'finished',
    over: true,
    endSummary: null,
    dashboard: {
      points: [
        {ratio: 1.2, verdict: 'kept', at: 1_790_000_000_500},
        {ratio: null, verdict: 'running', at: null},
      ],
      pointsOmitted: 4,
      failedStreak: 1,
      lastEventAt: null,
      attention: 'repeated_failure',
      best: {value: 287.4, metric: 'output_tokens_per_s', unit: null},
    },
  });
  expect(answer.runs.get('run-b')).toMatchObject({kind: 'unreadable', message: 'unreadable'});
});

test('property: a digest is over exactly when it has an ending or a closing, and no defined value is refused', () => {
  const outcomes = [
    RunEndOutcome.UNSPECIFIED,
    RunEndOutcome.ADOPTED,
    RunEndOutcome.KEPT_INPUT,
    RunEndOutcome.NO_WINNER,
    RunEndOutcome.ADOPTION_FAILED,
    RunEndOutcome.STOPPED,
    RunEndOutcome.DEADLINE,
    RunEndOutcome.FINISH_REFUSED,
    RunEndOutcome.FAILED,
    RunEndOutcome.FORCE_STOPPED,
  ];
  fc.assert(
    fc.property(
      fc.option(fc.constantFrom(...outcomes), {nil: undefined}),
      fc.constantFrom(
        RunClosing.UNSPECIFIED,
        RunClosing.FINISHED,
        RunClosing.FAILED,
        RunClosing.INTERRUPTED,
      ),
      fc.string(),
      (outcome, closing, summary) => {
        const entries = digestEntries(
          create(ListRunDigestsResponseSchema, {
            requestId: 'r',
            digests: [
              {
                runId: 'run-a',
                createdAt: created,
                result: {
                  case: 'digest',
                  value: {
                    closing,
                    ...(outcome === undefined ? {} : {ending: {outcome, summary}}),
                  },
                },
              },
            ],
          }),
        );
        const entry = entries.get('run-a');
        if (entry?.kind !== 'read') throw new Error('read required');
        expect(entry.over).toBe(outcome !== undefined || closing !== RunClosing.UNSPECIFIED);
        expect(entry.endSummary === null).toBe(outcome === undefined || summary.trim() === '');
      },
    ),
    {numRuns: 100},
  );
});
