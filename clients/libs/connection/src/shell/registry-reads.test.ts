import {describe, expect, test} from 'bun:test';
import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import type {HostId} from '@vibesys/hosts';
import {
  Accelerator,
  ClusterSource,
  DescribeMachineResponseSchema,
  HostReadRequestSchema,
  HostReadResponseSchema,
  ListComputeResponseSchema,
} from '@vibesys/proto';
import {registryReads, registryTransport, requireCheckout} from './registry-reads.js';

const HOST: HostId = {kind: 'ssh', alias: 'host-a'};

describe('requireCheckout', () => {
  test('refuses a missing checkout and a checkout that moved', () => {
    expect(() => requireCheckout(null, null)).toThrow('Set a checkout first');
    expect(() => requireCheckout('', null)).toThrow('Set a checkout first');
    expect(() => requireCheckout('/saved', '/other')).toThrow('checkout changed');
    expect(requireCheckout('/saved', null)).toBe('/saved');
    expect(requireCheckout('/saved', '/saved')).toBe('/saved');
  });
});

describe('registry reads', () => {
  test('an aborted signal never sends, and a moved checkout never sends', async () => {
    const controller = new AbortController();
    controller.abort();
    let sent = 0;
    const reads = registryReads({
      checkout: async () => '/saved',
      unary: async () => {
        sent += 1;
        return new Uint8Array();
      },
    });
    await expect(reads.describeMachine(HOST, null, controller.signal)).rejects.toThrow(
      'operation cancelled',
    );
    await expect(reads.listCompute(HOST, '/other', new AbortController().signal)).rejects.toThrow(
      'checkout changed',
    );
    expect(sent).toBe(0);
  });

  test('list compute and describe machine project source and docker without a second spelling', async () => {
    const seen: string[] = [];
    const reads = registryReads({
      checkout: async () => '/saved',
      unary: async (_host, bytes) => {
        const request = fromBinary(HostReadRequestSchema, bytes);
        seen.push(request.checkout);
        const method = request.method;
        if (method.case === 'listCompute') {
          return toBinary(
            HostReadResponseSchema,
            create(HostReadResponseSchema, {
              result: {
                case: 'listCompute',
                value: create(ListComputeResponseSchema, {
                  requestId: method.value.requestId,
                  settings: {
                    configPath: '/settings',
                    clusters: [
                      {
                        cluster: {
                          name: 'campus',
                          workspace: '/work',
                          reach: {value: {case: 'sshReach', value: {kind: 'ssh', host: 'host-a'}}},
                        },
                        source: ClusterSource.SLURM_TOML,
                      },
                    ],
                    modal: {tokenPresent: false},
                  },
                }),
              },
            }),
          );
        }
        if (method.case !== 'describeMachine') throw new Error(method.case ?? 'missing method');
        return toBinary(
          HostReadResponseSchema,
          create(HostReadResponseSchema, {
            result: {
              case: 'describeMachine',
              value: create(DescribeMachineResponseSchema, {
                requestId: method.value.requestId,
                machine: {
                  hardware: {cpuCores: 8n, accelerator: Accelerator.CPU},
                  docker: {
                    value: {case: 'dockerReachable', value: {reachable: true, version: '27.3.1'}},
                  },
                  harnessClis: ['codex'],
                  runs: {live: 0n, endedToday: 0n},
                },
              }),
            },
          }),
        );
      },
    });
    const signal = new AbortController().signal;
    expect(await reads.listCompute(HOST, '/saved', signal)).toMatchObject({
      config_path: '/settings',
      clusters: [
        {
          source: 'slurm_toml',
          cluster: {name: 'campus', reach: {kind: 'ssh', host: 'host-a'}, workspace: '/work'},
        },
      ],
      modal: {token_present: false, source: null},
    });
    expect(await reads.describeMachine(HOST, null, signal)).toMatchObject({
      hardware: {cpu_cores: 8, accelerator: 'cpu'},
      docker: {reachable: true, version: '27.3.1'},
      harness_clis: ['codex'],
      runs: {live: 0, ended_today: 0},
    });
    expect(seen).toEqual(['/saved', '/saved']);
  });

  test('a transport whose signal is already aborted does not send', async () => {
    const controller = new AbortController();
    controller.abort();
    const transport = registryTransport('/saved', async () => {
      throw new Error('sent');
    });
    await expect(transport.invoke(15, new Uint8Array(), 15000, controller.signal)).rejects.toThrow(
      'operation cancelled',
    );
  });
});
