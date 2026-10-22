import {expect, test} from 'bun:test';
import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import {LoginActor, registryClusters, registryProjects, registryStart} from '@vibesys/connection';
import type {HostId} from '@vibesys/hosts';
import {
  ClusterTestStage,
  MutateRequestSchema,
  MutateResponseSchema,
  ReadRequestSchema,
  WatchRequestSchema,
  WatchResponseSchema,
} from '@vibesys/proto';

const HOST: HostId = {kind: 'ssh', alias: 'host-a'};
const cluster = {name: 'lab', reach: {kind: 'login_node' as const}, workspace: '/work'};
const inputs = {
  project: '/work/project',
  task: 'task',
  harness: null,
  options: {},
  agents: {},
  measure_on: {kind: 'same_machine' as const},
  args: [],
};

/** The receipt ledger outlives client instances, just as it does on the host. */
class ReceiptGateway {
  readonly ids: string[] = [];
  readonly receipts = new Map<string, {request: Uint8Array; response: Uint8Array}>();
  checkout = async () => '/checkout';
  read = async (_host: HostId, bytes: Uint8Array): Promise<Uint8Array> => {
    throw new Error(`unexpected read: ${fromBinary(ReadRequestSchema, bytes).method.case}`);
  };
  mutate = async (_host: HostId, bytes: Uint8Array) => {
    const method = fromBinary(MutateRequestSchema, bytes).method;
    if (method.case === undefined) throw new Error('missing mutation');
    const requestId = method.value.requestId;
    this.ids.push(requestId);
    const prior = this.receipts.get(requestId);
    if (prior !== undefined) {
      if (
        prior.request.length !== bytes.length ||
        prior.request.some((byte, index) => byte !== bytes[index])
      ) {
        throw new Error('admission identity conflicts with a different request');
      }
      return prior.response;
    }
    const settings = {configPath: '/settings', modal: {tokenPresent: false}};
    const result = create(MutateResponseSchema);
    switch (method.case) {
      case 'startRun':
        Object.assign(
          result,
          create(MutateResponseSchema, {
            result: {
              case: 'startRun',
              value: {
                requestId,
                runId: `run-${this.receipts.size}`,
                instanceId: `instance-${this.receipts.size}`,
              },
            },
          }),
        );
        break;
      case 'putCluster':
        Object.assign(
          result,
          create(MutateResponseSchema, {
            result: {case: 'putCluster', value: {requestId, settings}},
          }),
        );
        break;
      case 'removeCluster':
        Object.assign(
          result,
          create(MutateResponseSchema, {
            result: {case: 'removeCluster', value: {requestId, settings}},
          }),
        );
        break;
      case 'testCluster':
        Object.assign(
          result,
          create(MutateResponseSchema, {
            result: {
              case: 'testCluster',
              value: {
                requestId,
                result: {
                  case: 'failed',
                  value: {stage: ClusterTestStage.REACH, detail: 'unreachable'},
                },
              },
            },
          }),
        );
        break;
      case 'createExample':
        Object.assign(
          result,
          create(MutateResponseSchema, {
            result: {case: 'createExample', value: {requestId, result: {case: 'taken', value: {}}}},
          }),
        );
        break;
      case 'copyProject':
        Object.assign(
          result,
          create(MutateResponseSchema, {
            result: {
              case: 'copyProject',
              value: {requestId, created: {project: '/work/new', tasks: ['task']}},
            },
          }),
        );
        break;
    }
    const response = toBinary(MutateResponseSchema, result);
    this.receipts.set(requestId, {request: bytes, response});
    return response;
  };
}

test('new actions across client instances never reuse durable admission identities', async () => {
  const gateway = new ReceiptGateway();
  const runs = new Set<string>();
  const signal = new AbortController().signal;
  // Rotating action order covers independent counters; two fresh instances pin the restart bug.
  for (let restart = 0; restart < 12; restart += 1) {
    const starts = registryStart({checkout: gateway.checkout, unary: gateway.mutate});
    const clusters = registryClusters({checkout: gateway.checkout, unary: gateway.mutate});
    const projects = registryProjects(gateway);
    const actions = [
      async () =>
        runs.add((await starts.start(HOST, {...inputs, task: `task-${restart}`}, signal)).runId),
      async () => clusters.put(HOST, cluster, signal),
      async () => clusters.remove(HOST, cluster.name, signal),
      async () => clusters.test(HOST, cluster, signal),
      async () => projects.create(HOST, 'queue', '/work/new', signal),
      async () => projects.copy(HOST, '/work/project', '/work/new', signal),
    ];
    for (let index = 0; index < actions.length; index += 1)
      await actions[(index + restart) % actions.length]?.();
    if (restart % 2 === 0) await actions[restart % actions.length]?.();
  }
  expect(new Set(gateway.ids).size).toBe(gateway.ids.length);
  expect(runs.size).toBe(14);
  for (const id of gateway.ids) expect(id).toMatch(/^[0-9a-f-]{36}$/);
});

test('login actions also get fresh identities after restarting the client', async () => {
  const ids: string[] = [];
  for (let restart = 0; restart < 8; restart += 1) {
    const actor = new LoginActor({
      checkout: async () => '/checkout',
      stream: async (_host, bytes, _signal, receive) => {
        const method = fromBinary(WatchRequestSchema, bytes).method;
        if (method.case !== 'login') throw new Error('expected login');
        const requestId = method.value.requestId;
        ids.push(requestId);
        receive(
          toBinary(
            WatchResponseSchema,
            create(WatchResponseSchema, {
              result: {
                case: 'login',
                value: {requestId, sequence: 1n, body: {case: 'started', value: {}}},
              },
            }),
          ),
        );
      },
    });
    await actor.start(HOST, 'claude');
    actor.close();
  }
  expect(new Set(ids).size).toBe(ids.length);
});
