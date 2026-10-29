import {expect, test} from 'bun:test';
import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import type {GatewayBridge} from '@vibesys/ipc';
import {
  GetBlobsRequestSchema,
  SkeletonRequestSchema,
  SkeletonResponseSchema,
  TailRequestSchema,
  TailResponseSchema,
} from '@vibesys/proto';
import {storeRunFixture} from '@vibesys/run-fixtures';
import {refusalOf} from '@vibesys/run-skeleton';
import {FakeRunTransport} from '@vibesys/run-skeleton/testing';
import {GrpcCode, GrpcError, grpcCode} from '@vibesys/transport';
import {gatewayTransport} from './gateway-transport.js';

/** The callback bridge over a faithful in-memory gateway; each call owns its cancellation. */
function bridgeFor(remote: FakeRunTransport): GatewayBridge {
  return {
    unary(_host, method, bytes) {
      const owner = new AbortController();
      return {result: remote.unary(method, bytes, owner.signal), cancel: () => owner.abort()};
    },
    stream(_host, method, bytes, receive, ended) {
      const owner = new AbortController();
      const follow = async (): Promise<void> => {
        try {
          for await (const frame of remote.stream(method, bytes, owner.signal)) receive(frame);
          ended();
        } catch (error) {
          ended(String(error), grpcCode(error));
        }
      };
      void follow();
      return {cancel: () => owner.abort()};
    },
  };
}
const SERVICE = '/vibesys.api.v1.HostService/';

test('the bridge preserves unary bytes and aborts a pending unary at its owner', async () => {
  const fixture = storeRunFixture();
  const remote = new FakeRunTransport(fixture);
  const transport = gatewayTransport(bridgeFor(remote), 'local');
  const owner = new AbortController();
  const bytes = await transport.unary(
    `${SERVICE}Skeleton`,
    toBinary(SkeletonRequestSchema, create(SkeletonRequestSchema, {runId: 'run-a'})),
    owner.signal,
  );
  expect(fromBinary(SkeletonResponseSchema, bytes).nextSeq).toBe(
    BigInt(fixture.records.length + 1),
  );
  remote.held.add(`${SERVICE}GetBlobs`);
  const pending = transport.unary(
    `${SERVICE}GetBlobs`,
    toBinary(
      GetBlobsRequestSchema,
      create(GetBlobsRequestSchema, {runId: 'run-a', sha256: Object.keys(fixture.blobs)}),
    ),
    owner.signal,
  );
  await remote.called(`${SERVICE}GetBlobs`);
  owner.abort();
  await expect(pending).rejects.toThrow('Cancelled');
  expect(remote.calls.at(-1)?.signal.aborted).toBe(true);
});

test('stream data, remote errors and owner cancellation cross the callback adapter', async () => {
  const remote = new FakeRunTransport(storeRunFixture());
  const transport = gatewayTransport(bridgeFor(remote), 'local');
  const owner = new AbortController();
  const request = toBinary(
    TailRequestSchema,
    create(TailRequestSchema, {runId: 'run-a', fromSeq: 1n}),
  );
  const iterator = transport
    .stream(`${SERVICE}Tail`, request, owner.signal)
    [Symbol.asyncIterator]();
  const first = await iterator.next();
  if (first.done) throw new Error('Tail data required');
  expect(fromBinary(TailResponseSchema, first.value).records.length).toBeGreaterThan(0);
  const pending = iterator.next();
  owner.abort();
  expect((await pending).done).toBe(true);
  expect(remote.calls.at(-1)?.signal.aborted).toBe(true);
  const failure = new AbortController();
  remote.failures.set(`${SERVICE}Tail`, new Error('Fault'));
  const failed = transport
    .stream(`${SERVICE}Tail`, request, failure.signal)
    [Symbol.asyncIterator]();
  await expect(failed.next()).rejects.toThrow('Fault');
  failure.abort();
});

test('a stream ended with a gRPC status rethrows that status to the reader', async () => {
  for (const code of [undefined, GrpcCode.NOT_FOUND, GrpcCode.INTERNAL]) {
    const remote = new FakeRunTransport(storeRunFixture());
    remote.fail(
      `${SERVICE}Tail`,
      code === undefined ? new Error('pipe closed') : new GrpcError(code, 'lstat: no such file'),
    );
    const transport = gatewayTransport(bridgeFor(remote), 'local');
    const read = async (): Promise<unknown> => {
      try {
        for await (const _ of transport.stream(
          `${SERVICE}Tail`,
          toBinary(TailRequestSchema, create(TailRequestSchema, {runId: 'run-a', fromSeq: 1n})),
          new AbortController().signal,
        ));
      } catch (error) {
        return error;
      }
      return undefined;
    };
    const error = await read();
    expect((error as {status?: number}).status).toBe(code);
    expect(refusalOf(error) === 'not-found').toBe(code === GrpcCode.NOT_FOUND);
  }
});
