/**
 * A host whose VibeSys predates the registry operations, seen through every registry client the
 * app has. The gateway types every command an older checkout rejects as unsupported; the Fake host
 * answers as the gateway does, and both are asserted against the recorded argparse output of a real
 * older checkout (`proto/testdata/older_host/older_host.json`). No failure carries the host's own
 * text in its message.
 */
import {expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {fromBinary, fromJson, toBinary} from '@bufbuild/protobuf';
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
import {commandOf, olderHost} from '@vibesys/connection/testing';
import type {HostId} from '@vibesys/hosts';
import {
  FailureCategory,
  HostCallRequestSchema,
  MutateRequestSchema,
  type MutateResponse,
  MutateResponseSchema,
  OperationFailed,
  ReadRequestSchema,
  type ReadResponse,
  ReadResponseSchema,
  WatchRequestSchema,
  type WatchResponse,
  WatchResponseSchema,
} from '@vibesys/proto';

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

/** What a real older checkout wrote for every host command the gateway runs. */
const corpus: {
  rejected: {command: string; exit_code: number; stderr: string}[];
  provided: string[];
} = JSON.parse(
  readFileSync(
    new URL('../../../../../proto/testdata/older_host/older_host.json', import.meta.url),
    'utf8',
  ),
);
const rejected = corpus.rejected.map(entry => entry.command);

const CALLS: readonly (readonly [string, string, () => Promise<unknown>])[] = [
  ['describe machine', 'describe-machine', () => reads.describeMachine(HOST, null, signal())],
  ['list compute', 'list-compute', () => reads.listCompute(HOST, null, signal())],
  [
    'inspect candidate',
    'inspect-candidate',
    () => setup.inspectCandidate(HOST, '/src/checkout', 1n, signal()),
  ],
  ['complete paths', 'complete-paths', () => setup.completePaths(HOST, '/work/', signal())],
  ['saved runs', 'list-saved-runs', () => savedRuns.list(HOST, '/work/p', signal())],
  ['put cluster', 'put-cluster', () => clusters.put(HOST, cluster, signal())],
  ['remove cluster', 'remove-cluster', () => clusters.remove(HOST, 'lab', signal())],
  ['test cluster', 'test-cluster', () => clusters.test(HOST, cluster, signal())],
  [
    'inspect destination',
    'inspect-destination',
    () => projects.destination(HOST, 'queue', '/work/q', signal()),
  ],
  ['create example', 'create-example', () => projects.create(HOST, 'queue', '/work/q', signal())],
  ['copy project', 'copy-project', () => projects.copy(HOST, '/work/p', '/work/q', signal())],
  ['start run', 'start-run', () => starts.start(HOST, inputs, signal())],
  ['login', 'login', () => logins.start(HOST, 'claude')],
];

for (const [name, command, call] of CALLS) {
  test(`${name} on an older host fails as needing an update, never with the host's text`, async () => {
    const error = await failureOf(call);
    expect(error).toBeInstanceOf(OperationFailed);
    const failed = error as OperationFailed;
    expect(rejected).toContain(command);
    expect(isHostUnsupported(error)).toBe(true);
    expect(failed.category).toBe(FailureCategory.UNSUPPORTED);
    expect(failed.message).toBe(HOST_UNSUPPORTED_MESSAGE);
    expect(failed.diagnostic).toContain(command);
  });
}

test('the Fake host answers exactly the commands a real older checkout rejects, for every method', async () => {
  const answered: string[] = [];
  const unsupported = (response: ReadResponse | MutateResponse | WatchResponse): boolean => {
    if (response.result.case !== 'failure') throw new Error('the older host did not fail');
    return response.result.value.category === FailureCategory.UNSUPPORTED;
  };
  for (const field of ReadRequestSchema.oneofs[0]?.fields ?? []) {
    const bytes = await olderHost.read(
      HOST,
      toBinary(ReadRequestSchema, fromJson(ReadRequestSchema, {[field.jsonName]: {}})),
    );
    expect(unsupported(fromBinary(ReadResponseSchema, bytes))).toBe(true);
    answered.push(commandOf(field.name));
  }
  for (const field of MutateRequestSchema.oneofs[0]?.fields ?? []) {
    const bytes = await olderHost.mutate(
      HOST,
      toBinary(MutateRequestSchema, fromJson(MutateRequestSchema, {[field.jsonName]: {}})),
    );
    expect(unsupported(fromBinary(MutateResponseSchema, bytes))).toBe(true);
    answered.push(commandOf(field.name));
  }
  for (const field of WatchRequestSchema.oneofs[0]?.fields ?? []) {
    const payloads: Uint8Array[] = [];
    await olderHost.watch(
      HOST,
      toBinary(WatchRequestSchema, fromJson(WatchRequestSchema, {[field.jsonName]: {}})),
      signal(),
      bytes => {
        payloads.push(bytes);
      },
    );
    expect(unsupported(fromBinary(WatchResponseSchema, payloads[0] ?? new Uint8Array()))).toBe(
      true,
    );
    answered.push(commandOf(field.name));
  }
  // Host calls ride the HostCall envelope, which the Fake does not serve; every other rejected
  // command is a Read, Mutate or Watch method it answers.
  const hostCalls = (HostCallRequestSchema.oneofs[0]?.fields ?? []).map(field =>
    commandOf(field.name),
  );
  const unserved = new Set(hostCalls);
  expect([...answered].sort()).toEqual(
    [...rejected.filter(command => !unserved.has(command))].sort(),
  );
  // A host call is either provided by the older checkout or rejected by it, never both or neither.
  expect([...hostCalls].sort()).toEqual(
    [...corpus.provided, ...rejected.filter(command => unserved.has(command))].sort(),
  );
});

test('no failure message carries what the host wrote, whatever its category', () => {
  const texts = [
    ...corpus.rejected.map(entry => entry.stderr),
    'vibesys host list-compute exited 2\nusage: vibesys host [-h] ...',
    'Traceback (most recent call last):',
    '',
  ];
  for (const category of Object.values(FailureCategory).filter(
    value => typeof value === 'number',
  )) {
    for (const text of texts) {
      const error = new OperationFailed(category as number, text);
      expect(error.diagnostic).toBe(text);
      if (text !== '') expect(error.message).not.toContain(text);
      expect(error.message).not.toMatch(/usage|invalid choice|Traceback/i);
      expect(isHostUnsupported(error)).toBe(category === FailureCategory.UNSUPPORTED);
    }
  }
  expect(isHostUnsupported(new Error('gateway closed'))).toBe(false);
});
