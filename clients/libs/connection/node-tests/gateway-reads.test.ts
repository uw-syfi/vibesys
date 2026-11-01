/**
 * Reads asked of a host while an identical read is in flight wait for that one answer. A desktop
 * launch asks the same few questions from several screens at once, and each wire read starts a
 * host process, so what is asked twice must cost once (and a mutation never shares).
 */
import {test} from 'node:test';
import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import {
  encodeHostMutate,
  encodeHostRead,
  ListTasksRequestSchema,
  ReadResponseSchema,
  RemoveClusterRequestSchema,
} from '@vibesys/proto';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {FakeClock} from '@vibesys/transport/testing';
import {expect} from 'expect';
import {GatewayCalls, MUTATE_METHOD, READ_METHOD} from '../src/index.js';
// test-isolation: Node HTTP/2 needs the gateway Fake alone; legacy contract exports load bun:test.
import {FakeGateway, type FakeRead} from '../src/testing/fake-gateway.js';

const HOST = 'local';
const PROJECTS = ['project-a', 'project-b', 'project-c'] as const;

/** A read of one project's tasks; the project is the question, so two projects differ in bytes. */
const asking = (project: string): Uint8Array =>
  encodeHostRead(
    '/checkout',
    'ListTasks',
    toBinary(ListTasksRequestSchema, create(ListTasksRequestSchema, {requestId: 'read', project})),
  );

const askedProject = (read: FakeRead): string => {
  if (read.request.method.case !== 'listTasks') throw new Error('not a task read');
  return read.request.method.value.project;
};

/** The reply a host gives to a read of `project`, which names the project it answers. */
const answer = (project: string): Uint8Array =>
  toBinary(
    ReadResponseSchema,
    create(ReadResponseSchema, {
      result: {
        case: 'listTasks',
        value: {
          requestId: 'read',
          tasks: {version: 1n, projectRoot: project, tasks: [{name: 'task'}]},
        },
      },
    }),
  );

const answeredProject = (bytes: Uint8Array): string => {
  const {result} = fromBinary(ReadResponseSchema, bytes);
  if (result.case !== 'listTasks' || result.value.tasks === undefined)
    throw new Error('not a task answer');
  return result.value.tasks.projectRoot;
};

/** Reads arrive in the order they were issued over one connection, so a last read marks the rest. */
async function readsUpTo(fake: FakeGateway, last: string): Promise<FakeRead[]> {
  const received: FakeRead[] = [];
  for (;;) {
    const read = await fake.nextRead();
    received.push(read);
    if (askedProject(read) === last) return received;
  }
}

function peer() {
  const fake = new FakeGateway();
  const clock = new FakeClock();
  const calls = new GatewayCalls(() => fake, clock.schedule);
  return {
    fake,
    calls,
    ask: (project: string, signal = new AbortController().signal) =>
      calls.unary(HOST, READ_METHOD, asking(project), signal),
    end: () => {
      calls.close();
      fake.close();
    },
  };
}

test('identical reads in flight share one wire read, and each caller gets its own answer', {
  timeout: 30000,
}, async () => {
  for (const seed of SEEDS.slice(0, 40)) {
    const random = new Seeded(seed);
    const projects = Array.from({length: random.int(10) + 1}, () => random.pick(PROJECTS));
    const {fake, ask, end} = peer();
    try {
      const callers = projects.map(project => ask(project));
      const barrier = ask('barrier');
      const received = await readsUpTo(fake, 'barrier');
      // One read per distinct question, however many callers asked it.
      expect(received.slice(0, -1).map(askedProject).sort()).toEqual([...new Set(projects)].sort());
      for (const read of received) read.respond(answer(askedProject(read)));
      const results = await Promise.all(callers);
      expect(results.map(answeredProject)).toEqual(projects);
      await barrier;
      // Callers do not share bytes: one that edits its answer leaves the others' alone.
      const [first, ...rest] = results;
      if (first !== undefined && rest.length > 0) {
        const before = rest.map(bytes => [...bytes]);
        first.fill(0);
        expect(rest.map(bytes => [...bytes])).toEqual(before);
      }
    } finally {
      end();
    }
  }
});

test('an answered read is not kept: the next identical ask reads again', {
  timeout: 15000,
}, async () => {
  const {fake, ask, end} = peer();
  try {
    const first = ask('project-a');
    (await fake.nextRead()).respond(answer('project-a'));
    expect(answeredProject(await first)).toBe('project-a');
    const second = ask('project-a');
    const again = await fake.nextRead();
    again.respond(answer('project-a'));
    expect(answeredProject(await second)).toBe('project-a');
    expect(fake.readsReceived).toHaveLength(2);
  } finally {
    end();
  }
});

test('a caller that cancels leaves the read to the others, and the last to cancel ends it', {
  timeout: 15000,
}, async () => {
  const {fake, ask, end} = peer();
  try {
    const owners = [new AbortController(), new AbortController(), new AbortController()];
    const callers = owners.map(owner => ask('project-a', owner.signal).catch(() => 'cancelled'));
    const barrier = ask('barrier');
    const received = await readsUpTo(fake, 'barrier');
    expect(received.map(askedProject)).toEqual(['project-a', 'barrier']);
    const [read, gate] = received;
    if (read === undefined || gate === undefined) throw new Error('no read arrived');
    gate.respond(answer('barrier'));
    owners[0]?.abort();
    owners[1]?.abort();
    expect(await callers[0]).toBe('cancelled');
    expect(await callers[1]).toBe('cancelled');
    // The third still waits on the same read, which was not cancelled under it.
    read.respond(answer('project-a'));
    expect(answeredProject((await callers[2]) as Uint8Array)).toBe('project-a');
    await barrier;

    // With every caller gone the read is cancelled, and a new ask does not join it.
    const last = [new AbortController(), new AbortController()];
    const gone = last.map(owner => ask('project-b', owner.signal).catch(() => 'cancelled'));
    const open = await fake.nextRead();
    for (const owner of last) owner.abort();
    expect(await Promise.all(gone)).toEqual(['cancelled', 'cancelled']);
    await open.closed;
    const fresh = ask('project-b');
    const next = await fake.nextRead();
    next.respond(answer('project-b'));
    expect(answeredProject(await fresh)).toBe('project-b');
  } finally {
    end();
  }
});

test('a mutation is never shared, however alike two of them are', {timeout: 15000}, async () => {
  const fake = new FakeGateway();
  const clock = new FakeClock();
  const calls = new GatewayCalls(() => fake, clock.schedule);
  const mutation = encodeHostMutate(
    '/checkout',
    'RemoveCluster',
    toBinary(
      RemoveClusterRequestSchema,
      create(RemoveClusterRequestSchema, {requestId: 'remove', name: 'cluster'}),
    ),
  );
  try {
    const both = [1, 2].map(() =>
      calls
        .unary(HOST, MUTATE_METHOD, mutation, new AbortController().signal)
        .catch(error => String(error)),
    );
    const [one, two] = [await fake.nextMutate(), await fake.nextMutate()];
    one.end(13, 'refused');
    two.end(13, 'refused');
    expect(
      (await Promise.all(both)).every(
        outcome => typeof outcome === 'string' && outcome.includes('refused'),
      ),
    ).toBe(true);
  } finally {
    calls.close();
    fake.close();
  }
});
