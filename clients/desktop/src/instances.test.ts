import {describe, expect, test} from 'bun:test';
import {PROTOCOL_VERSION} from '@vibesys/backend-client';
import {checkAttachment} from './attachment.js';
import {
  parseDetachedLaunch,
  parseInstanceList,
  parseInstanceRecord,
  parseStopResult,
  RecordError,
} from './instances.js';
import {FakeHost} from './testing/fake-host.js';
import {fakeRecord} from './testing/fake-record.js';

const ID = '0123456789ab';
const SOCKET = '/run/user/1000/vibesys/runs/0123456789ab/control.sock';

/** A small seeded generator (fast-check is not a dependency; see the testing skill). */
function generator(seed: number): () => number {
  let state = seed >>> 0;
  return () => {
    state = (state * 1_664_525 + 1_013_904_223) >>> 0;
    return state / 2 ** 32;
  };
}

function generatedInstanceId(random: () => number, valid: boolean): string {
  const alphabet = valid ? '0123456789abcdef' : '0123456789abcdefABCDEFgh-';
  const length = valid ? 12 : Math.floor(random() * 18);
  let candidate = Array.from(
    {length},
    () => alphabet[Math.floor(random() * alphabet.length)] ?? '-',
  ).join('');
  if (!valid && /^[0-9a-f]{12}$/.test(candidate)) candidate = `${candidate.slice(0, 11)}G`;
  return candidate;
}

describe('registry records', () => {
  test('a record of this protocol parses into the instance', () => {
    const record = parseInstanceRecord(fakeRecord(ID, SOCKET));
    expect(record).toEqual({
      kind: 'compatible',
      instance: {
        id: ID,
        status: 'serving',
        socketPath: SOCKET,
        projectRoot: '/home/user/project',
        runId: null,
        pid: 4242,
        startedAt: 1_700_000_000,
        hostname: 'node-1',
        vibesysVersion: '0.0.0+fake',
        vibesysRoot: null,
      },
    });
  });

  test('vibesys_root is read when present and null when an older server leaves it out', () => {
    const record = parseInstanceRecord({
      ...fakeRecord(ID, SOCKET, 1),
      vibesys_root: '/home/me/src/vibesys',
    });
    expect(record.kind === 'compatible' && record.instance.vibesysRoot).toBe(
      '/home/me/src/vibesys',
    );
    expect(() => parseInstanceRecord({...fakeRecord(ID, SOCKET, 1), vibesys_root: 3})).toThrow(
      'record.vibesys_root must be a string or null',
    );
  });

  test('a record of another protocol is incompatible, whatever else it carries', () => {
    for (const version of [0, 2, 99, '1', null]) {
      const record = {
        ...fakeRecord(ID, SOCKET, 1),
        protocol_version: version,
        new_field: [1],
        pid: 'x',
      };
      expect(parseInstanceRecord(record)).toEqual({
        kind: 'incompatible',
        id: ID,
        protocolVersion: version,
        vibesysVersion: '0.0.0+fake',
      });
    }
  });

  test('a malformed record of this protocol names the offending field', () => {
    const cases: [Record<string, unknown>, string][] = [
      [{extra: 1}, 'record.extra is not a known field'],
      [{id: 'ZZ'}, 'record.id must be 12 lowercase hex digits'],
      [{status: 'gone'}, 'record.status'],
      [{pid: -1}, 'record.pid must be a positive integer'],
      [{socket_path: 3}, 'record.socket_path must be a string'],
    ];
    for (const [change, message] of cases) {
      expect(() => parseInstanceRecord({...fakeRecord(ID, SOCKET), ...change})).toThrow(message);
    }
    expect(() => parseInstanceList({version: 1, instances: {}})).toThrow(RecordError);
  });

  test('unverified entries are authoritative instance ids and name their indexed error path', () => {
    const valid = 'ba9876543210';
    expect(parseInstanceList({version: 1, instances: [], unverified: [ID, valid]})).toEqual({
      records: [],
      unverified: [ID, valid],
    });
    for (const invalid of ['ABCDEF123456', '123', 'gggggggggggg', 12, null]) {
      expect(() =>
        parseInstanceList({version: 1, instances: [], unverified: [ID, invalid]}),
      ).toThrow('listing.unverified[1] must be 12 lowercase hex digits');
    }
  });

  test('every generated unverified value is accepted exactly when it is 12 lowercase hex digits', () => {
    const random = generator(1_833);
    for (let sample = 0; sample < 300; sample += 1) {
      const valid = random() < 0.5;
      const candidate = generatedInstanceId(random, valid);
      const document = {version: 1, instances: [], unverified: [ID, candidate]};
      if (valid) {
        expect(parseInstanceList(document).unverified).toEqual([ID, candidate]);
      } else {
        expect(() => parseInstanceList(document)).toThrow('listing.unverified[1]');
      }
    }
  });

  test('a version 2 record always names its run, and a listing reports records it cannot read', () => {
    const named = {...fakeRecord(ID, SOCKET), version: 2, run_id: 'run-7'};
    const record = parseInstanceRecord(named);
    expect(record.kind === 'compatible' && record.instance.runId).toBe('run-7');
    expect(() => parseInstanceRecord({...named, run_id: null})).toThrow('record.run_id');

    const listing = parseInstanceList({
      version: 2,
      instances: [named],
      unreadable: [{id: 'ba9876543210', version: 9}],
      unverified: [],
    });
    expect(listing.unreadable).toEqual([{id: 'ba9876543210', version: 9}]);
    expect(() => parseInstanceList({version: 2, instances: [], unreadable: [{id: 3}]})).toThrow(
      'listing.unreadable[0].id',
    );
  });
});

describe('version gate', () => {
  function attached(
    protocolVersion: number,
    options: {readonly instances?: readonly object[]; readonly unverified?: readonly string[]} = {},
  ) {
    const host = new FakeHost({
      command: () => ({
        code: 0,
        stdout: JSON.stringify({
          version: 1,
          instances: options.instances ?? [fakeRecord(ID, SOCKET, protocolVersion)],
          unverified: options.unverified ?? [],
        }),
        stderr: '',
      }),
    });
    return {
      host,
      hostName: 'gpu-box',
      vibesysCommand: 'uv run --project ~/src/vibesys vibesys',
      instanceId: ID,
      endpoint: {socketPath: SOCKET},
    };
  }

  test('a mismatch names both versions and the host command, never a schema error', async () => {
    const outcome = await checkAttachment(attached(PROTOCOL_VERSION + 1), false);
    expect(outcome.ok).toBe(false);
    if (outcome.ok) return;
    expect(outcome.cause).toBe('version-skew');
    expect(outcome.detail).toContain(`protocol version ${PROTOCOL_VERSION}`);
    expect(outcome.detail).toContain(`protocol version ${PROTOCOL_VERSION + 1}`);
    expect(outcome.detail).toContain('uv run --project ~/src/vibesys vibesys');
    expect(outcome.detail).toContain('gpu-box');
  });

  test('a matching run passes, and a run missing from the registry is gone', async () => {
    expect(await checkAttachment(attached(PROTOCOL_VERSION), false)).toEqual({ok: true});
    const run = attached(PROTOCOL_VERSION);
    expect(await checkAttachment({...run, instanceId: 'ffffffffffff'}, false)).toMatchObject({
      ok: false,
      cause: 'run-gone',
    });
  });

  test('compatible evidence wins an overlap; otherwise unverified stays unknown', async () => {
    expect(await checkAttachment(attached(PROTOCOL_VERSION, {unverified: [ID]}), false)).toEqual({
      ok: true,
    });
    expect(
      await checkAttachment(
        attached(PROTOCOL_VERSION, {
          instances: [fakeRecord(ID, SOCKET, PROTOCOL_VERSION + 1)],
          unverified: [ID],
        }),
        false,
      ),
    ).toEqual({ok: false, cause: 'link', detail: `the host could not confirm run ${ID}`});
  });
});

describe('detached launch and stop documents', () => {
  test('a record is a start, a document with an outcome is a failure', () => {
    expect(parseDetachedLaunch(fakeRecord(ID, SOCKET)).kind).toBe('started');
    const failed = parseDetachedLaunch({
      version: 1,
      outcome: 'failed',
      code: 'run_already_live',
      stage: 'resume',
      message: 'run r is live',
      exit_code: 1,
      live_instance: fakeRecord(ID, SOCKET),
    });
    expect(failed.kind === 'failed' && failed.failure.liveInstance?.kind).toBe('compatible');
    expect(() =>
      parseDetachedLaunch({
        version: 1,
        outcome: 'failed',
        code: 'x',
        stage: 's',
        message: 'm',
        exit_code: 2,
        extra: 1,
      }),
    ).toThrow('launch.extra is not a known field');
  });

  test('every stop outcome parses, and nothing else does', () => {
    for (const outcome of [
      'stopped',
      'stopping',
      'not_running',
      'still_running',
      'unsupported',
    ] as const) {
      for (const route of ['control_socket', 'signal', null] as const) {
        expect(parseStopResult({version: 1, id: ID, outcome, route})).toEqual({
          id: ID,
          outcome,
          route,
        });
      }
    }
    expect(() => parseStopResult({version: 1, id: ID, outcome: 'paused'})).toThrow('stop.outcome');
    expect(() => parseStopResult({version: 1, id: ID, outcome: 'stopped', route: 'x'})).toThrow(
      'stop.route',
    );
  });

  test('a socket path must be absolute', () => {
    for (const socket of ['relative.sock', '-oProxyCommand=x', '~/x.sock', '']) {
      expect(() => parseInstanceRecord(fakeRecord(ID, socket))).toThrow('socket_path');
    }
  });
});
