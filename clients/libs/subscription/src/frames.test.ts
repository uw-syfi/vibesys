import {expect, test} from 'bun:test';
import {create} from '@bufbuild/protobuf';
import {
  SubscribeResponseSchema,
  type TopicEntry,
  TopicEntrySchema,
  TopicFailureCode,
} from '@vibesys/proto';
import {type FailureReason, type StreamEvent, streamEvent} from './index.js';

const ENTRY: TopicEntry = create(TopicEntrySchema, {key: 'machine'});

function of(frame: Parameters<typeof create<typeof SubscribeResponseSchema>>[1]): StreamEvent {
  return streamEvent(create(SubscribeResponseSchema, frame), 77);
}

test('each frame means one thing to the machine, stamped with when it arrived', () => {
  expect(of({frame: {case: 'heartbeat', value: {}}})).toEqual({type: 'beat', at: 77});
  expect(of({frame: {case: 'synced', value: {}}})).toEqual({type: 'synced', at: 77});
  expect(of({frame: {case: 'removed', value: 'a'}})).toEqual({type: 'removed', key: 'a', at: 77});
  expect(of({frame: {case: 'entry', value: ENTRY}})).toEqual({
    type: 'entry',
    entry: ENTRY,
    at: 77,
  });
});

test('a heartbeat is never a snapshot, and a failure names whether another stream may follow', () => {
  expect(of({frame: {case: 'heartbeat', value: {}}}).type).not.toBe('synced');
  const reasons: [TopicFailureCode, FailureReason][] = [
    [TopicFailureCode.UNSUPPORTED, 'unsupported'],
    [TopicFailureCode.RESOURCE_EXHAUSTED, 'resource_exhausted'],
    [TopicFailureCode.SOURCE_FAILED, 'source_failed'],
    [TopicFailureCode.INTERNAL, 'internal'],
  ];
  for (const [code, reason] of reasons)
    expect(of({frame: {case: 'failed', value: {code, detail: 'x'}}})).toEqual({
      type: 'refused',
      reason,
    });
});

test('a frame with no case, or a failure with no code, breaks the protocol', () => {
  const invalid: StreamEvent = {type: 'refused', reason: 'invalid_frame'};
  expect(of({})).toEqual(invalid);
  expect(
    of({frame: {case: 'failed', value: {code: TopicFailureCode.UNSPECIFIED, detail: ''}}}),
  ).toEqual(invalid);
});
