import {expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import {HostService, SkeletonRequestSchema, SkeletonResponseSchema} from '@vibesys/proto';
import {FakeClock} from '@vibesys/transport/testing';
import {refusalOf} from './index.js';
import {
  chooseKeyframe,
  GATEWAY_KEYFRAME_CANDIDATES,
  GATEWAY_MAX_STORED_KEYFRAME,
  GATEWAY_REPLY_BYTES,
  SimGateway,
} from './testing.js';

/** The cases the service's own tests run (gateway/internal/server/skeleton_contract_test.go). */
const CONTRACT = JSON.parse(
  readFileSync(
    new URL('../../../../proto/testdata/skeleton-contract/cases.json', import.meta.url),
    'utf8',
  ),
) as {
  constants: {keyframeCandidates: number; replyBytes: number; maxStoredKeyframe: number};
  selection: {
    name: string;
    checkpoints: {through: number; fits: boolean; stored?: 'over'}[];
    chosen: number;
  }[];
  logs: {
    name: string;
    log: 'pending' | 'unindexed';
    skeleton: {
      status: number;
      complete: boolean;
      committedSeq: number;
      nextSeq: number;
      records: number;
      keyframe: boolean;
    };
  }[];
};
const SKELETON = `/${HostService.typeName}/${HostService.method.skeleton.name}`;

test('the simulated gateway has the service constants', () => {
  expect(GATEWAY_KEYFRAME_CANDIDATES).toBe(CONTRACT.constants.keyframeCandidates);
  expect(GATEWAY_REPLY_BYTES).toBe(CONTRACT.constants.replyBytes);
  expect(GATEWAY_MAX_STORED_KEYFRAME).toBe(CONTRACT.constants.maxStoredKeyframe);
});

test('the simulated gateway chooses a checkpoint as the service does', () => {
  for (const c of CONTRACT.selection) {
    // A checkpoint is usable when its reply fits and its stored form is within the read limit.
    const fits = new Map(
      c.checkpoints.map(point => [point.through, point.fits && point.stored !== 'over']),
    );
    expect(
      chooseKeyframe([...fits.keys()], through => fits.get(through) === true) ?? 0,
      c.name,
    ).toBe(c.chosen);
  }
});

test('the simulated gateway answers a log the service has not got as the service does', async () => {
  for (const c of CONTRACT.logs) {
    const clock = new FakeClock();
    const gateway = new SimGateway({records: [], log: c.log}, clock, {rttMs: 0, bytesPerMs: 1e9});
    const request = toBinary(SkeletonRequestSchema, create(SkeletonRequestSchema, {runId: 'a'}));
    const pending = gateway.unary(SKELETON, request, new AbortController().signal);
    await Promise.resolve();
    while (clock.runNext()) await Promise.resolve();
    if (c.skeleton.status !== 0) {
      const error = await pending.catch((reason: unknown) => reason);
      expect(refusalOf(error) !== undefined, c.name).toBe(true);
      expect((error as {status: number}).status, c.name).toBe(c.skeleton.status);
      continue;
    }
    const reply = fromBinary(SkeletonResponseSchema, await pending);
    expect(
      {
        complete: reply.complete,
        committedSeq: Number(reply.committedSeq),
        nextSeq: Number(reply.nextSeq),
        records: reply.records.length,
        keyframe: reply.keyframe !== undefined,
      },
      c.name,
    ).toEqual({
      complete: c.skeleton.complete,
      committedSeq: c.skeleton.committedSeq,
      nextSeq: c.skeleton.nextSeq,
      records: c.skeleton.records,
      keyframe: c.skeleton.keyframe,
    });
  }
});
