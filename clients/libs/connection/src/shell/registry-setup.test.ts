import {describe, expect, test} from 'bun:test';
import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import type {HostId} from '@vibesys/hosts';
import {
  CandidateState,
  CompletePathsResponseSchema,
  InspectCandidateResponseSchema,
  ReadRequestSchema,
  ReadResponseSchema,
} from '@vibesys/proto';
import {registrySetup} from './registry-setup.js';

const HOST: HostId = {kind: 'ssh', alias: 'host-a'};
const HASH = 'ab'.repeat(32);

describe('registry setup', () => {
  test('an aborted signal never sends', async () => {
    const controller = new AbortController();
    controller.abort();
    let sent = 0;
    const setup = registrySetup({
      unary: async () => {
        sent += 1;
        return new Uint8Array();
      },
    });
    await expect(setup.inspectCandidate(HOST, '/candidate', 1n, controller.signal)).rejects.toThrow(
      'operation cancelled',
    );
    await expect(setup.completePaths(HOST, '/manual/', controller.signal)).rejects.toThrow(
      'operation cancelled',
    );
    expect(sent).toBe(0);
  });

  test('a different selection revision is stale and never becomes a checkout', async () => {
    const setup = registrySetup({
      unary: async (_host, bytes) => {
        const request = fromBinary(ReadRequestSchema, bytes);
        const method = request.method;
        if (method.case !== 'inspectCandidate') throw new Error(method.case ?? 'empty');
        return toBinary(
          ReadResponseSchema,
          create(ReadResponseSchema, {
            result: {
              case: 'inspectCandidate',
              value: create(InspectCandidateResponseSchema, {
                requestId: method.value.requestId,
                selectionRevision: method.value.selectionRevision + 1n,
                schemaHash: HASH,
                state: CandidateState.NOT_FOUND,
                minimumVersion: '0.1.1',
              }),
            },
          }),
        );
      },
    });
    await expect(
      setup.inspectCandidate(HOST, '/candidate', 4n, new AbortController().signal),
    ).rejects.toThrow('stale selection');
  });

  test('projects a found candidate and drops git from a path listing', async () => {
    const seen: string[] = [];
    const setup = registrySetup({
      unary: async (_host, bytes) => {
        const request = fromBinary(ReadRequestSchema, bytes);
        seen.push(request.checkout);
        const method = request.method;
        if (method.case === 'inspectCandidate') {
          return toBinary(
            ReadResponseSchema,
            create(ReadResponseSchema, {
              result: {
                case: 'inspectCandidate',
                value: create(InspectCandidateResponseSchema, {
                  requestId: method.value.requestId,
                  selectionRevision: method.value.selectionRevision,
                  schemaHash: HASH,
                  state: CandidateState.FOUND,
                  minimumVersion: '0.1.1',
                  vibesysCheckout: true,
                  identity: {
                    root: '/candidate',
                    version: '0.1.1',
                    commit: 'a'.repeat(40),
                    compatible: true,
                    vibesysProject: true,
                    gitRepository: true,
                  },
                }),
              },
            }),
          );
        }
        if (method.case === 'completePaths') {
          return toBinary(
            ReadResponseSchema,
            create(ReadResponseSchema, {
              result: {
                case: 'completePaths',
                value: create(CompletePathsResponseSchema, {
                  requestId: method.value.requestId,
                  directory: '/manual',
                  truncated: false,
                  entries: [
                    {
                      name: 'project',
                      path: '/manual/project',
                      vibesysCheckout: true,
                      vibesysProject: false,
                      gitRepository: true,
                    },
                  ],
                }),
              },
            }),
          );
        }
        throw new Error(method.case ?? 'empty');
      },
    });
    await expect(
      setup.inspectCandidate(HOST, '/candidate', 2n, new AbortController().signal),
    ).resolves.toEqual({
      state: 'found',
      root: '/candidate',
      version: '0.1.1',
      commit: 'a'.repeat(40),
      dirty: null,
      compatible: true,
    });
    await expect(
      setup.completePaths(HOST, 'relative', new AbortController().signal),
    ).resolves.toEqual({
      directory: '/manual',
      truncated: false,
      entries: [{name: 'project', path: '/manual/project', checkout: true, project: false}],
    });
    expect(seen).toEqual(['/candidate', '~']);
  });
});
