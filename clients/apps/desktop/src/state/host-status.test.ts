import {create} from '@bufbuild/protobuf';
import {describe, expect, it} from '@effect/vitest';
import {BuildVersionsSchema} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';
import {type HostConnectionState, refusalCauseOf} from './connection.js';
import {type Fault, REFUSAL_CAUSES} from './fault.js';
import type {FeedStatus} from './feed.js';
import {
  HOST_STATUS_KINDS,
  type HostStatusInput,
  hostStatusOf,
  hostStatusRank,
} from './host-status.js';

const link: Fault = {kind: 'Unavailable', detail: 'link lost', retryable: true};
const daemon = create(BuildVersionsSchema, {binaryVersion: 'dev'});

const cause = fc.constantFrom(...REFUSAL_CAUSES);

/** Every way the link stands, a refusal in every cause included. */
const connections: fc.Arbitrary<HostConnectionState> = fc.oneof(
  fc.constantFrom<HostConnectionState>(
    {_tag: 'Idle'},
    {_tag: 'Connecting', attempt: 1},
    {_tag: 'Waiting', attempt: 2, fault: link, retryAtMs: 5},
    {_tag: 'Incompatible', side: 'client-too-old'},
    {_tag: 'Incompatible', side: 'daemon-too-old', daemon},
    {_tag: 'Unusable', fault: link},
    {_tag: 'Unusable', fault: {kind: 'Refused', detail: 'no', retryable: false}},
    {_tag: 'Ready', epoch: 1, daemon},
  ),
  cause.map(
    (c): HostConnectionState => ({
      _tag: 'Unusable',
      fault: {kind: 'Refused', detail: 'no', retryable: false, cause: c},
    }),
  ),
  cause.map(
    (c): HostConnectionState => ({
      _tag: 'Ready',
      epoch: 1,
      daemon,
      blocked: {cause: c, detail: 'old'},
    }),
  ),
);
const feeds: fc.Arbitrary<FeedStatus> = fc.constantFrom<FeedStatus>(
  {_tag: 'Opening'},
  {_tag: 'Live'},
  {_tag: 'Restarting', why: 'gap'},
  {_tag: 'Failed', fault: link},
);
const inputs: fc.Arbitrary<HostStatusInput> = fc.record({
  connection: connections,
  feed: feeds,
  hasData: fc.boolean(),
  staleForMs: fc.option(fc.nat(), {nil: null}),
});

const rank = (input: HostStatusInput): number => hostStatusRank(hostStatusOf(input).kind);
const ready = (feed: FeedStatus, hasData: boolean): HostStatusInput => ({
  connection: {_tag: 'Ready', epoch: 1, daemon},
  feed,
  hasData,
  staleForMs: null,
});

describe('the status of a host', () => {
  it('is exactly one of the closed kinds, whatever the link, the feed, the data and the cause', () => {
    fc.assert(
      fc.property(inputs, input => {
        const status = hostStatusOf(input);
        expect(HOST_STATUS_KINDS).toContain(status.kind);
        // The cause is the host's own, and only a refusal has one.
        expect(status.cause).toBe(
          status.kind === 'refused' ? refusalCauseOf(input.connection) : null,
        );
        // An age only comes with a stale host.
        if (status.kind !== 'stale') expect(status.staleForMs).toBeNull();
      }),
    );
  });

  it('is live exactly when the link is ready, nothing blocks it and the feed delivers', () => {
    fc.assert(
      fc.property(inputs, input => {
        const live =
          input.connection._tag === 'Ready' &&
          input.connection.blocked === undefined &&
          input.feed._tag === 'Live';
        expect(hostStatusOf(input).kind === 'live').toBe(live);
      }),
    );
  });

  it('never reads better as the feed gets worse over a ready link', () => {
    fc.assert(
      fc.property(fc.boolean(), hasData => {
        const order: FeedStatus[] = [
          {_tag: 'Live'},
          {_tag: 'Opening'},
          {_tag: 'Failed', fault: link},
        ];
        const ranks = order.map(feed => rank(ready(feed, hasData)));
        expect(ranks).toEqual([...ranks].sort((a, b) => a - b));
      }),
    );
  });

  it('never reads a link that is not ready better than one that is, and a dead link no better than any other', () => {
    fc.assert(
      fc.property(inputs, fc.boolean(), (input, hasData) => {
        const base = {...input, hasData};
        const best = rank(ready({_tag: 'Live'}, hasData));
        expect(rank(base)).toBeGreaterThanOrEqual(best);
        const dead =
          input.connection._tag === 'Incompatible' || input.connection._tag === 'Unusable';
        if (dead) {
          for (const feed of [{_tag: 'Live'}, {_tag: 'Failed', fault: link}] as const)
            for (const other of [true, false])
              expect(rank(base)).toBeGreaterThanOrEqual(rank(ready(feed, other)));
        }
      }),
    );
  });

  it('never reads a host that cannot run a run better than the same host with nothing in the way', () => {
    fc.assert(
      fc.property(cause, feeds, fc.boolean(), (c, feed, hasData) => {
        const blocked: HostStatusInput = {
          ...ready(feed, hasData),
          connection: {_tag: 'Ready', epoch: 1, daemon, blocked: {cause: c, detail: 'old'}},
        };
        expect(rank(blocked)).toBeGreaterThanOrEqual(rank(ready(feed, hasData)));
      }),
    );
  });

  it('keeps the age only for a stale host', () => {
    const stale = hostStatusOf({
      connection: {_tag: 'Waiting', attempt: 1, fault: link, retryAtMs: 5},
      feed: {_tag: 'Live'},
      hasData: true,
      staleForMs: 180_000,
    });
    expect(stale).toEqual({kind: 'stale', cause: null, staleForMs: 180_000});
  });
});
