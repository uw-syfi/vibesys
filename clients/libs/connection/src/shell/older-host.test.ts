/**
 * A host whose VibeSys predates the registry operations, seen through every registry client the
 * app has: each one fails with the one typed unsupported error, never with the host's usage dump.
 */
import {expect, test} from 'bun:test';
import {
  HOST_UNSUPPORTED_MESSAGE,
  isHostUnsupported,
  LoginActor,
  registryClusters,
  registryProjects,
  registryReads,
  registrySavedRuns,
  registrySetup,
  registryStart,
} from '@vibesys/connection';
import {olderHost, USAGE_DUMP} from '@vibesys/connection/testing';
import type {HostId} from '@vibesys/hosts';
import {FailureCategory, OperationFailed} from '@vibesys/proto';

const HOST: HostId = {kind: 'ssh', alias: 'host-a'};
const signal = () => new AbortController().signal;
const checkout = async () => '/src/checkout';
const unary = olderHost.read;
const cluster = {
  name: 'lab',
  reach: {kind: 'ssh' as const, host: 'host-a'},
  workspace: '/work',
  partition: 'gpu',
  nodes: 2,
  time_limit: '01:00:00',
  sbatch_options: [],
};
const inputs = {
  project: '/work/p',
  task: 'task',
  harness: null,
  options: {},
  agents: {},
  measure_on: {kind: 'same_machine' as const},
  args: [],
};

async function failureOf(call: () => Promise<unknown>): Promise<unknown> {
  try {
    await call();
  } catch (error) {
    return error;
  }
  throw new Error('the call did not fail');
}

const reads = registryReads({checkout, unary});
const setup = registrySetup({unary});
const clusters = registryClusters({checkout, unary: olderHost.mutate});
const projects = registryProjects({checkout, read: olderHost.read, mutate: olderHost.mutate});
const starts = registryStart({checkout, unary: olderHost.mutate});
const savedRuns = registrySavedRuns({checkout, unary});
const logins = new LoginActor({checkout, stream: olderHost.watch});

const CALLS: readonly (readonly [string, () => Promise<unknown>])[] = [
  ['describe machine', () => reads.describeMachine(HOST, null, signal())],
  ['list compute', () => reads.listCompute(HOST, null, signal())],
  ['inspect candidate', () => setup.inspectCandidate(HOST, '/src/checkout', 1n, signal())],
  ['complete paths', () => setup.completePaths(HOST, '/work/', signal())],
  ['saved runs', () => savedRuns.list(HOST, '/work/p', signal())],
  ['put cluster', () => clusters.put(HOST, cluster, signal())],
  ['remove cluster', () => clusters.remove(HOST, 'lab', signal())],
  ['test cluster', () => clusters.test(HOST, cluster, signal())],
  ['inspect destination', () => projects.destination(HOST, 'queue', '/work/q', signal())],
  ['create example', () => projects.create(HOST, 'queue', '/work/q', signal())],
  ['copy project', () => projects.copy(HOST, '/work/p', '/work/q', signal())],
  ['start run', () => starts.start(HOST, inputs, signal())],
  ['login', () => logins.start(HOST, 'claude')],
];

for (const [name, call] of CALLS) {
  test(`${name} on an older host is the typed unsupported failure and shows no usage dump`, async () => {
    const error = await failureOf(call);
    expect(error).toBeInstanceOf(OperationFailed);
    expect(isHostUnsupported(error)).toBe(true);
    expect((error as OperationFailed).category).toBe(FailureCategory.UNSUPPORTED);
    expect((error as OperationFailed).message).toBe(HOST_UNSUPPORTED_MESSAGE);
    expect((error as OperationFailed).message).not.toContain(USAGE_DUMP);
  });
}

test('only the unsupported category loses the host text', () => {
  const others = [
    FailureCategory.INVALID_INPUT,
    FailureCategory.CONFIGURATION,
    FailureCategory.NOT_FOUND,
    FailureCategory.INTERNAL,
  ];
  for (const category of others) {
    const error = new OperationFailed(category, 'the host said so');
    expect(error.message).toBe('the host said so');
    expect(isHostUnsupported(error)).toBe(false);
  }
});
