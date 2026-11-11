import fc from 'fast-check';
import {describe, expect, it} from 'vitest';
import {
  applyKeyedTopicEvent,
  dormantKeyedTopic,
  type KeyedTopicEvent,
  type KeyedTopicState,
} from './keyed-topic.js';

type Change = Extract<KeyedTopicEvent<number>, {readonly _tag: 'Entry' | 'Removed'}>;

const KEYS = ['run-a', 'run-b', 'run-c'] as const;
const change: fc.Arbitrary<Change> = fc.oneof(
  fc.record({
    _tag: fc.constant('Entry' as const),
    key: fc.constantFrom(...KEYS),
    value: fc.integer(),
  }),
  fc.record({_tag: fc.constant('Removed' as const), key: fc.constantFrom(...KEYS)}),
);

const fold = (
  events: readonly KeyedTopicEvent<number>[],
  from: KeyedTopicState<number> = dormantKeyedTopic(),
  maxKeys: number = KEYS.length,
) => events.reduce((state, event) => applyKeyedTopicEvent(state, event, maxKeys), from);

const replay = (changes: readonly Change[]) => {
  const values = new Map<string, number>();
  for (const event of changes) {
    if (event._tag === 'Entry') values.set(event.key, event.value);
    else values.delete(event.key);
  }
  return values;
};

describe('the keyed topic fold', () => {
  it('publishes a snapshot atomically, then applies changes to the live map', () => {
    fc.assert(
      fc.property(fc.array(change), fc.array(change), (snapshot, after) => {
        const reading = fold([{_tag: 'Open'}, ...snapshot]);
        expect(reading._tag).toBe('Connecting');
        const live = applyKeyedTopicEvent(reading, {_tag: 'Synced'}, KEYS.length);
        expect(live).toEqual({_tag: 'Live', value: replay(snapshot)});
        expect(fold(after, live)).toEqual({_tag: 'Live', value: replay([...snapshot, ...after])});
      }),
    );
  });

  it('keeps the last whole map stale until a reconnect snapshot replaces it without ghosts', () => {
    fc.assert(
      fc.property(fc.array(change), fc.array(change), (first, replacement) => {
        const live = fold([{_tag: 'Open'}, ...first, {_tag: 'Synced'}]);
        const stale = applyKeyedTopicEvent(live, {_tag: 'Stale', why: 'disconnected'}, KEYS.length);
        const refreshing = fold([{_tag: 'Open'}, ...replacement], stale);
        expect(refreshing).toMatchObject({
          _tag: 'Stale',
          value: replay(first),
          why: 'refreshing',
        });
        expect(applyKeyedTopicEvent(refreshing, {_tag: 'Synced'}, KEYS.length)).toEqual({
          _tag: 'Live',
          value: replay(replacement),
        });
      }),
    );
  });

  it('ignores late data after a stream ended, and distinguishes refusal from failure', () => {
    fc.assert(
      fc.property(change, event => {
        const refused = fold([{_tag: 'Open'}, {_tag: 'Refused', refusal: 'unsupported'}]);
        expect(applyKeyedTopicEvent(refused, event, KEYS.length)).toBe(refused);
        const failed = fold([
          {_tag: 'Open'},
          {_tag: 'Failed', failure: {_tag: 'InvalidFrame', detail: 'wrong topic'}},
        ]);
        expect(applyKeyedTopicEvent(failed, event, KEYS.length)).toBe(failed);
        expect(refused._tag).toBe('Refused');
        expect(failed._tag).toBe('Failed');
      }),
    );
  });

  it('fails a stream that exceeds its key bound and forgets everything when stopped', () => {
    fc.assert(
      fc.property(fc.integer(), fc.integer(), (one, two) => {
        const over = fold(
          [
            {_tag: 'Open'},
            {_tag: 'Entry', key: 'run-a', value: one},
            {_tag: 'Entry', key: 'run-b', value: two},
          ],
          undefined,
          1,
        );
        expect(over).toEqual({
          _tag: 'Failed',
          failure: {_tag: 'InvalidFrame', detail: 'more than 1 key'},
        });
        expect(applyKeyedTopicEvent(over, {_tag: 'Stop'}, 1)).toEqual({_tag: 'Dormant'});
      }),
    );
  });
});
