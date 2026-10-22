import {expect, test} from 'bun:test';
import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import type {HostId} from '@vibesys/hosts';
import {
  ClusterTestFailedSchema,
  ClusterTestStage,
  ComputeSettingsSchema,
  MutateRequestSchema,
  MutateResponseSchema,
  PutClusterResponseSchema,
  TestClusterResponseSchema,
} from '@vibesys/proto';
import {registryClusters} from './registry-clusters.js';

const HOST: HostId = {kind: 'ssh', alias: 'host-a'};

const cluster = {
  name: 'lab',
  reach: {kind: 'ssh' as const, host: 'host-a'},
  workspace: '/work',
  partition: 'gpu',
  nodes: 2,
  time_limit: '01:00:00',
  sbatch_options: ['--gres=gpu:1'],
};

test('a v2 put sends the cluster and reads the host settings back', async () => {
  let sent: Uint8Array = new Uint8Array();
  const clusters = registryClusters({
    checkout: async () => '/src/checkout',
    unary: async (_host, bytes) => {
      sent = bytes;
      return toBinary(
        MutateResponseSchema,
        create(MutateResponseSchema, {
          result: {
            case: 'putCluster',
            value: create(PutClusterResponseSchema, {
              requestId: 'cluster-1',
              settings: create(ComputeSettingsSchema, {
                configPath: '/settings',
                modal: {tokenPresent: false},
              }),
            }),
          },
        }),
      );
    },
  });

  const settings = await clusters.put(HOST, cluster, new AbortController().signal);
  const request = fromBinary(MutateRequestSchema, sent);

  expect(request.checkout).toBe('/src/checkout');
  expect(request.method.case).toBe('putCluster');
  if (request.method.case !== 'putCluster') return;
  expect(request.method.value.requestId).toBe('cluster-1');
  expect(request.method.value.cluster?.name).toBe('lab');
  expect(request.method.value.cluster?.reach?.value.case).toBe('sshReach');
  expect(request.method.value.cluster?.workspace).toBe('/work');
  expect(request.method.value.cluster?.partition).toBe('gpu');
  expect(request.method.value.cluster?.nodes).toBe(2n);
  expect(request.method.value.cluster?.timeLimit).toBe('01:00:00');
  expect(request.method.value.cluster?.sbatchOptions).toEqual(['--gres=gpu:1']);
  expect(settings.config_path).toBe('/settings');
  expect(settings.modal.token_present).toBe(false);
});

test('a v2 test keeps the host failure', async () => {
  const clusters = registryClusters({
    checkout: async () => '/src/checkout',
    unary: async () =>
      toBinary(
        MutateResponseSchema,
        create(MutateResponseSchema, {
          result: {
            case: 'testCluster',
            value: create(TestClusterResponseSchema, {
              requestId: 'cluster-1',
              result: {
                case: 'failed',
                value: create(ClusterTestFailedSchema, {
                  stage: ClusterTestStage.REACH,
                  detail: 'could not reach host-a',
                }),
              },
            }),
          },
        }),
      ),
  });

  const result = await clusters.test(
    HOST,
    {name: 'lab', reach: {kind: 'login_node'}, workspace: '/work'},
    new AbortController().signal,
  );

  expect(result).toEqual({
    outcome: 'failed',
    stage: 'reach',
    detail: 'could not reach host-a',
  });
});
