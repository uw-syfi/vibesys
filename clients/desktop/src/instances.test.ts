import {describe, expect, test} from 'bun:test';
import {PROTOCOL_VERSION} from '@vibesys/backend-client';
import {checkAttachment} from './attachment.js';
import {parseInstanceList, parseInstanceRecord, RecordError} from './instances.js';
import {FakeHost} from './testing/fake-host.js';
import {fakeRecord} from './testing/fake-record.js';

const ID = '0123456789ab';
const SOCKET = '/run/user/1000/vibesys/runs/0123456789ab/control.sock';

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
      },
    });
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
      [{id: 'ZZ'}, 'record.id must be 12 hex digits'],
      [{status: 'gone'}, 'record.status'],
      [{pid: -1}, 'record.pid must be a positive integer'],
      [{socket_path: 3}, 'record.socket_path must be a string'],
    ];
    for (const [change, message] of cases) {
      expect(() => parseInstanceRecord({...fakeRecord(ID, SOCKET), ...change})).toThrow(message);
    }
    expect(() => parseInstanceList({version: 1, instances: {}})).toThrow(RecordError);
  });
});

describe('version gate', () => {
  function attached(protocolVersion: number) {
    const host = new FakeHost({
      command: () => ({
        code: 0,
        stdout: JSON.stringify({version: 1, instances: [fakeRecord(ID, SOCKET, protocolVersion)]}),
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
});
