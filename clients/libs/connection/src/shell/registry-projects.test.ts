import {expect, test} from 'bun:test';
import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import type {HostId} from '@vibesys/hosts';
import {
  CreateExampleResponseSchema,
  DestinationState,
  InspectDestinationResponseSchema,
  MutateRequestSchema,
  MutateResponseSchema,
  ReadRequestSchema,
  ReadResponseSchema,
} from '@vibesys/proto';
import {registryProjects} from './registry-projects.js';

const HOST: HostId = {kind: 'ssh', alias: 'host-a'};

test('a v2 destination read names the example and returns the host state', async () => {
  let sent: Uint8Array = new Uint8Array();
  const projects = registryProjects({
    checkout: async () => '/src/checkout',
    read: async (_host, bytes) => {
      sent = bytes;
      return toBinary(
        ReadResponseSchema,
        create(ReadResponseSchema, {
          result: {
            case: 'inspectDestination',
            value: create(InspectDestinationResponseSchema, {
              requestId: 'project-1',
              state: DestinationState.ABSENT,
            }),
          },
        }),
      );
    },
    mutate: async () => new Uint8Array(),
  });

  const found = await projects.destination(
    HOST,
    'queue',
    '/work/queue',
    new AbortController().signal,
  );
  const request = fromBinary(ReadRequestSchema, sent);

  expect(request.method.case).toBe('inspectDestination');
  if (request.method.case !== 'inspectDestination') return;
  expect(request.method.value.example).toBe('queue');
  expect(request.method.value.destination).toBe('/work/queue');
  expect(found).toEqual({state: 'absent', project: null});
});

test('a v2 create keeps a taken destination', async () => {
  let sent: Uint8Array = new Uint8Array();
  const projects = registryProjects({
    checkout: async () => '/src/checkout',
    read: async () => new Uint8Array(),
    mutate: async (_host, bytes) => {
      sent = bytes;
      return toBinary(
        MutateResponseSchema,
        create(MutateResponseSchema, {
          result: {
            case: 'createExample',
            value: create(CreateExampleResponseSchema, {
              requestId: 'project-1',
              result: {case: 'taken', value: {}},
            }),
          },
        }),
      );
    },
  });

  const created = await projects.create(HOST, 'queue', '/work/queue', new AbortController().signal);
  const request = fromBinary(MutateRequestSchema, sent);

  expect(created).toEqual({kind: 'taken'});
  expect(request.method.case).toBe('createExample');
});
