import {expect, test} from 'bun:test';
import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import {
  HOST_UNSUPPORTED_MESSAGE,
  HostCallRequestSchema,
  HostCallResponseSchema,
  IntentAction,
  IntentStep,
  RecordSchema,
  SteerMode,
  SteerTargetKind,
  TerminateRunOutcome,
} from '@vibesys/proto';
import {
  configuredHostCall,
  hostIntentRequest,
  hostIntentResponse,
  IntentRefused,
  intentAdmissionWarning,
  intentOutcomes,
  runIntent,
  runReference,
} from '@vibesys/runs/intents';

const identity = {runId: 'run-1', requestId: 'r-1', projectRoot: '/project', expectedEpoch: 1n};

test('all visible controls use typed defaults and preserve identity through the pipes', () => {
  for (const action of ['pause', 'pauseNow', 'steer'] as const) {
    const request = runIntent(
      {...identity, ...runReference(identity.projectRoot, identity.runId)},
      action,
      'guidance',
    );
    const envelope = fromBinary(
      HostCallRequestSchema,
      configuredHostCall(hostIntentRequest(request), '/src/vibesys'),
    );
    expect(envelope.checkout).toBe('/src/vibesys');
    expect(envelope.method.case).toBe('postIntent');
    if (envelope.method.case !== 'postIntent') throw new Error('wrong method');
    expect(envelope.method.value).toEqual(request);
    expect(request.intent.case).toBe(action);
    if (request.intent.case === 'steer') {
      expect(request.intent.value.mode).toBe(SteerMode.NEXT_TURN);
      expect(request.intent.value.target?.kind).toBe(SteerTargetKind.RUN);
      expect(request.intent.value.text).toBe('guidance');
    }
  }
});

test('admission replies must match the request id', () => {
  const bytes = toBinary(
    HostCallResponseSchema,
    create(HostCallResponseSchema, {
      result: {case: 'postIntent', value: {requestId: 'r-1', orderedReader: true}},
    }),
  );
  expect(hostIntentResponse(bytes, 'r-1').orderedReader).toBe(true);
  expect(() => hostIntentResponse(bytes, 'r-2')).toThrow('invalid admission reply');
  expect(() => hostIntentResponse(new Uint8Array(), 'r-1')).toThrow();
});

test('intent and successor-epoch facts are history, with refusal reasons preserved', () => {
  const record = create(RecordSchema, {
    seq: 1n,
    body: {
      case: 'intent',
      value: {
        controlId: 'r-1',
        action: IntentAction.STEER,
        step: IntentStep.REFUSED,
        reason: 'role ended',
      },
    },
  });
  const ignored = create(RecordSchema, {seq: 2n});
  const result = intentOutcomes([
    toBinary(RecordSchema, record),
    toBinary(RecordSchema, ignored),
    toBinary(
      RecordSchema,
      create(RecordSchema, {seq: 3n, epoch: 2, body: {case: 'epochStarted', value: {epoch: 2}}}),
    ),
  ]);
  expect(result).toHaveLength(2);
  expect(result[1]?.source).toBe('epoch-started');
  expect(result[1]?.action).toBe(IntentAction.RESUME);
  expect(result[0]?.reason).toBe('role ended');
});

test('typed HostCall refusals remain distinct from malformed or unconfirmed replies', () => {
  for (const code of [
    'invalid_input',
    'inbox_closed',
    'request_too_large',
    'invalid_method',
    'host_unsupported',
  ]) {
    for (const stderr of ['', 'details']) {
      const bytes = toBinary(
        HostCallResponseSchema,
        create(HostCallResponseSchema, {
          result: {
            case: 'configurationFailed',
            value: {
              code,
              stage: 'post_intent',
              message: 'Request refused',
              stderr,
              exitCode: 2,
            },
          },
        }),
      );
      expect(() => hostIntentResponse(bytes, 'r-1')).toThrow(IntentRefused);
      let refusal: IntentRefused | undefined;
      try {
        hostIntentResponse(bytes, 'r-1');
      } catch (error) {
        refusal = error as IntentRefused;
      }
      // The host's stderr is for logs; a host that lacks the command says it needs an update.
      expect(refusal?.diagnostic).toBe(stderr);
      expect(refusal?.message).toBe(
        code === 'host_unsupported' ? HOST_UNSUPPORTED_MESSAGE : 'Request refused',
      );
    }
  }
});

test('a host process loss or deadline remains unconfirmed rather than refused', () => {
  for (const code of ['admission_unconfirmed', 'host_command_failed', 'invalid_host_response']) {
    const bytes = toBinary(
      HostCallResponseSchema,
      create(HostCallResponseSchema, {
        result: {case: 'configurationFailed', value: {code, message: 'Connection lost'}},
      }),
    );
    let failure: unknown;
    try {
      hostIntentResponse(bytes, 'r-1');
    } catch (error) {
      failure = error;
    }
    expect(failure).toBeInstanceOf(Error);
    expect(failure).not.toBeInstanceOf(IntentRefused);
    expect(String(failure)).toContain('same request id');
  }
});

test('a control reference requires both its project path and run identity', () => {
  for (const project of ['', ' ', '\n'])
    expect(() => runReference(project, 'run-1')).toThrow('projectRoot');
  expect(() => runReference('/project', '')).toThrow('runId');
  expect(runReference('/project', 'run-1')).toMatchObject({
    projectRoot: '/project',
    runId: 'run-1',
  });
});

test('every process outcome remains distinct and never confirms control application', () => {
  const warnings = new Set<string | null>();
  for (const outcome of [
    TerminateRunOutcome.WRITER_RELEASED,
    TerminateRunOutcome.IDENTITY_CHANGED,
    TerminateRunOutcome.UNSUPPORTED,
    TerminateRunOutcome.REQUESTED_UNCONFIRMED,
    TerminateRunOutcome.OBSERVATION_UNKNOWN,
    TerminateRunOutcome.PROCESS_ABSENT,
  ]) {
    const bytes = toBinary(
      HostCallResponseSchema,
      create(HostCallResponseSchema, {
        result: {
          case: 'postIntent',
          value: {requestId: 'r-1', orderedReader: true, termination: {requestId: 'r-1', outcome}},
        },
      }),
    );
    const response = hostIntentResponse(bytes, 'r-1');
    const warning = intentAdmissionWarning(response);
    expect(warning).not.toBeNull();
    expect(warning).not.toMatch(/\bstopped\b|\bapplied\b/);
    warnings.add(warning);
  }
  expect(warnings.size).toBe(6);
  for (const [outcome, requestId] of [
    [TerminateRunOutcome.UNSPECIFIED, 'r-1'],
    [999, 'r-1'],
    [TerminateRunOutcome.UNSUPPORTED, 'another'],
  ] as const) {
    const bytes = toBinary(
      HostCallResponseSchema,
      create(HostCallResponseSchema, {
        result: {
          case: 'postIntent',
          value: {
            requestId: 'r-1',
            termination: {requestId, outcome: outcome as TerminateRunOutcome},
          },
        },
      }),
    );
    expect(() => hostIntentResponse(bytes, 'r-1')).toThrow();
  }
});
