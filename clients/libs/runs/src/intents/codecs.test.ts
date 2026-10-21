import {expect, test} from 'bun:test';
import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import {
  HostCallRequestSchema,
  HostCallResponseSchema,
  IntentAction,
  IntentStep,
  RecordSchema,
  ResumeWhen,
  SteerMode,
  SteerTargetKind,
  StopMode,
  TailResponseSchema,
} from '@vibesys/proto';
import {
  configuredHostCall,
  hostIntentRequest,
  hostIntentResponse,
  intentTailResponse,
  runIntent,
} from '@vibesys/runs/intents';

const identity = {runId: 'run-1', requestId: 'r-1', projectRoot: '/project'};

test('all visible controls use typed defaults and preserve identity through the pipes', () => {
  for (const action of ['pause', 'resume', 'stop', 'steer'] as const) {
    const request = runIntent(identity, action, 'guidance');
    const envelope = fromBinary(
      HostCallRequestSchema,
      configuredHostCall(hostIntentRequest(request), '/src/vibesys'),
    );
    expect(envelope.checkout).toBe('/src/vibesys');
    expect(envelope.method.case).toBe('postIntent');
    if (envelope.method.case !== 'postIntent') throw new Error('wrong method');
    expect(envelope.method.value).toEqual(request);
    expect(request.intent.case).toBe(action);
    if (request.intent.case === 'resume') expect(request.intent.value.when).toBe(ResumeWhen.NOW);
    if (request.intent.case === 'stop') expect(request.intent.value.mode).toBe(StopMode.DRAIN);
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

test('only IntentState records are outcomes, with refusal reasons preserved', () => {
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
  const result = intentTailResponse(
    toBinary(
      TailResponseSchema,
      create(TailResponseSchema, {
        records: [toBinary(RecordSchema, record), toBinary(RecordSchema, ignored)],
      }),
    ),
  );
  expect(result).toHaveLength(1);
  expect(result[0]?.reason).toBe('role ended');
});
