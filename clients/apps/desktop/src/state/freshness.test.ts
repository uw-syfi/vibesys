import {create} from '@bufbuild/protobuf';
import {describe, expect, it} from '@effect/vitest';
import {BuildVersionsSchema} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';
import type {HostConnectionState} from './connection.js';
import type {Fault} from './fault.js';
import type {FeedStatus} from './feed.js';
import {freshnessOf} from './freshness.js';

const fault: Fault = {kind: 'Unavailable', detail: 'link lost', retryable: true};
const daemon = create(BuildVersionsSchema, {
  binaryVersion: 'dev',
  protocolVersion: 1,
  minProtocolVersion: 1,
});

const ready: HostConnectionState = {_tag: 'Ready', epoch: 1, daemon};
const notReady: fc.Arbitrary<HostConnectionState> = fc.constantFrom<HostConnectionState>(
  {_tag: 'Idle'},
  {_tag: 'Connecting', attempt: 0},
  {_tag: 'Waiting', attempt: 2, fault, retryAtMs: 5000},
  {_tag: 'Incompatible', side: 'daemon-too-old', daemon},
  {_tag: 'Unusable', fault},
);
const terminal: fc.Arbitrary<HostConnectionState> = fc.constantFrom<HostConnectionState>(
  {_tag: 'Incompatible', side: 'client-too-old'},
  {_tag: 'Unusable', fault},
);
const feeds: fc.Arbitrary<FeedStatus> = fc.constantFrom<FeedStatus>(
  {_tag: 'Opening'},
  {_tag: 'Live'},
  {_tag: 'Restarting', why: 'gap'},
  {_tag: 'Restarting', why: 'reconnect'},
  {_tag: 'Failed', fault},
);

describe('freshness', () => {
  it('is stale, never empty, whenever data is shown and the connection is not Ready', () => {
    fc.assert(
      fc.property(notReady, feeds, (connection, feed) => {
        expect(freshnessOf(connection, feed, true)).toBe('stale');
      }),
    );
  });

  it('is unavailable only when nothing is shown and nothing can be read until the user acts', () => {
    fc.assert(
      fc.property(terminal, feeds, (connection, feed) => {
        expect(freshnessOf(connection, feed, false)).toBe('unavailable');
      }),
    );
    expect(freshnessOf({_tag: 'Connecting', attempt: 0}, {_tag: 'Opening'}, false)).toBe('empty');
    expect(freshnessOf(ready, {_tag: 'Failed', fault}, false)).toBe('unavailable');
  });

  it('is live exactly when the connection is Ready and the feed is Live', () => {
    fc.assert(
      fc.property(fc.oneof(notReady, fc.constant(ready)), feeds, fc.boolean(), (c, f, hasData) => {
        const live = c._tag === 'Ready' && f._tag === 'Live';
        expect(freshnessOf(c, f, hasData) === 'live').toBe(live);
      }),
    );
  });

  it('is catching up only with data on screen and a Ready connection whose feed is not delivering', () => {
    expect(freshnessOf(ready, {_tag: 'Restarting', why: 'gap'}, true)).toBe('catching-up');
    expect(freshnessOf(ready, {_tag: 'Opening'}, false)).toBe('empty');
  });
});
