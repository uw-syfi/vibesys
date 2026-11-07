import {create} from '@bufbuild/protobuf';
import {describe, expect, it} from '@effect/vitest';
import {BuildVersionsSchema} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';
import type {HostConnectionState} from '../connection.js';
import type {FeedStatus} from '../feed.js';
import {hostStatusOf} from '../host-status.js';
import {machineRow} from './machine-rows.js';

const fault = {kind: 'TransportFailed', detail: 'link lost', retryable: true} as const;

const connections: readonly HostConnectionState[] = [
  {_tag: 'Idle'},
  {_tag: 'Connecting', attempt: 1},
  {_tag: 'Waiting', attempt: 2, fault, retryAtMs: 5},
  {_tag: 'Incompatible', side: 'client-too-old'},
  {_tag: 'Unusable', fault},
  {_tag: 'Ready', epoch: 1, daemon: create(BuildVersionsSchema)},
];

const feeds: readonly FeedStatus[] = [
  {_tag: 'Opening'},
  {_tag: 'Live'},
  {_tag: 'Failed', fault},
] as const;

describe('a resource row', () => {
  it('is the host as the app knows it: its name and the one status every screen shows of it', () => {
    fc.assert(
      fc.property(
        fc.constantFrom(...connections),
        fc.constantFrom(...feeds),
        fc.boolean(),
        fc.option(fc.nat(), {nil: null}),
        (connection, runsFeed, hasData, staleForMs) => {
          const row = machineRow({
            key: 'ssh:host-a',
            label: 'host-a',
            connection,
            runsFeed,
            hasData,
            staleForMs,
          });
          expect(row).toMatchObject({key: 'ssh:host-a', label: 'host-a'});
          expect(row.status).toEqual(
            hostStatusOf({connection, feed: runsFeed, hasData, staleForMs}),
          );
        },
      ),
    );
  });
});

describe('a host that refuses for good', () => {
  const refused = {
    kind: 'Refused',
    detail: 'the state directory is on nfs',
    retryable: false,
    cause: 'state-network-filesystem',
  } as const;
  const row = (connection: HostConnectionState) =>
    machineRow({
      key: 'ssh:host-a',
      label: 'host-a',
      connection,
      runsFeed: {_tag: 'Opening'},
      hasData: false,
      staleForMs: null,
    }).status;

  it('is refused, and says why, where a host that cannot be reached is unreachable and says nothing', () => {
    expect(row({_tag: 'Unusable', fault: refused})).toMatchObject({
      kind: 'refused',
      cause: 'state-network-filesystem',
    });
    expect(row({_tag: 'Unusable', fault})).toMatchObject({kind: 'unreachable', cause: null});
    expect(row({_tag: 'Connecting', attempt: 1}).cause).toBeNull();
  });
});
