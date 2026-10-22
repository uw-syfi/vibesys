/**
 * A host whose VibeSys predates the registry operations, seen through every registry client the
 * app has. The gateway types only the version handshake as unsupported; any other command fails as
 * an ordinary failure with the host's own diagnostic. The Fake host answers as the gateway does,
 * and both are asserted against the shared corpus of the commands an older host is reported to lack.
 */
import {expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {fromBinary, fromJson, toBinary} from '@bufbuild/protobuf';
import {
  isHostUnsupported,
  LoginActor,
  registryClusters,
  registryProjects,
  registryReads,
  registrySavedRuns,
  registrySetup,
  registryStart,
} from '@vibesys/connection';
import {
  COMMANDS_REPORTED_UNSUPPORTED,
  commandOf,
  olderHost,
  USAGE_DUMP,
} from '@vibesys/connection/testing';
import type {HostId} from '@vibesys/hosts';
import {
  FailureCategory,
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

/** The commands the gateway's classifier reports as unsupported when a checkout lacks them. */
const corpus: {commands: string[]} = JSON.parse(
  readFileSync(
    new URL('../../../../../proto/testdata/older_host/unsupported_commands.json', import.meta.url),
    'utf8',
  ),
);

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
  test(`${name} on an older host fails as the gateway types it, never as a bare usage dump`, async () => {
    const error = await failureOf(call);
    expect(error).toBeInstanceOf(OperationFailed);
    const failed = error as OperationFailed;
    if (corpus.commands.includes(command)) {
      expect(isHostUnsupported(error)).toBe(true);
      expect(failed.category).toBe(FailureCategory.UNSUPPORTED);
      expect(failed.message).toContain('update it');
    } else {
      expect(isHostUnsupported(error)).toBe(false);
      expect(failed.category).toBe(FailureCategory.INTERNAL);
      expect(failed.message).toContain(`vibesys host`);
    }
    expect(failed.message).not.toBe(USAGE_DUMP);
  });
}

test('the Fake host reports unsupported exactly the corpus commands, for every method', async () => {
  expect([...COMMANDS_REPORTED_UNSUPPORTED].sort()).toEqual([...corpus.commands].sort());
  const answered: [string, FailureCategory][] = [];
  const failureOf = (response: ReadResponse | MutateResponse | WatchResponse): FailureCategory => {
    if (response.result.case !== 'failure') throw new Error('the older host did not fail');
    return response.result.value.category;
  };
  const request = (schema: typeof ReadRequestSchema, name: string): Uint8Array =>
    toBinary(schema, fromJson(schema, {[name]: {}}));
  for (const field of ReadRequestSchema.oneofs[0]?.fields ?? []) {
    const bytes = await olderHost.read(HOST, request(ReadRequestSchema, field.jsonName));
    answered.push([commandOf(field.name), failureOf(fromBinary(ReadResponseSchema, bytes))]);
  }
  for (const field of MutateRequestSchema.oneofs[0]?.fields ?? []) {
    const bytes = await olderHost.mutate(
      HOST,
      toBinary(MutateRequestSchema, fromJson(MutateRequestSchema, {[field.jsonName]: {}})),
    );
    answered.push([commandOf(field.name), failureOf(fromBinary(MutateResponseSchema, bytes))]);
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
    answered.push([
      commandOf(field.name),
      failureOf(fromBinary(WatchResponseSchema, payloads[0] ?? new Uint8Array())),
    ]);
  }
  expect(answered.length).toBeGreaterThan(18);
  for (const [command, category] of answered) {
    const reported = corpus.commands.includes(command);
    expect([command, category]).toEqual([
      command,
      reported ? FailureCategory.UNSUPPORTED : FailureCategory.INTERNAL,
    ]);
  }
  for (const command of corpus.commands) {
    expect(answered.some(([answeredCommand]) => answeredCommand === command)).toBe(true);
  }
});

test('only the unsupported category, or a feature the helper did not negotiate, is unsupported', () => {
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
  expect(isHostUnsupported(new OperationFailed(FailureCategory.UNSUPPORTED, 'update it'))).toBe(
    true,
  );
  expect(isHostUnsupported(new Error('gateway closed'))).toBe(false);
});
