import {describe, it} from 'node:test';
import {BackendClientError} from './errors.js';
import {parseChannelEnd, parseHelperMessage} from './host-helper.js';
import {expect} from './test-support/expect.js';

const RECORD = {
  version: 4,
  id: '0123456789ab',
  status: 'serving',
  phase: {status: 'failed', stage: null, reason: {code: 'x', stage: 'startup', message: 'm'}},
  socket_path: '/tmp/r.sock',
  log_path: '/tmp/r.log',
  project_root: '/p',
  run_id: 'run-1',
  pid: 7,
  started_at: 1.5,
  ended_at: null,
  hostname: 'h',
  protocol_version: 1,
  vibesys_version: '0.1',
  vibesys_root: null,
};

/** One complete message of each shape the desktop reads; each must parse as it is. */
const VALID: readonly Record<string, unknown>[] = [
  {
    type: 'hello',
    request_id: 'h',
    protocol_version: 1,
    features: ['instances_watch', 'paths_complete'],
    vibesys_version: '0.1',
    checkout: {root: '/src/vibesys', commit: null, dirty: null},
  },
  {
    type: 'instances.changed',
    watch_id: 'w',
    listing: {
      version: 4,
      instances: [RECORD],
      ended: [],
      unreadable: [{id: 'abcdefabcdef', version: null}],
      unverified: [],
    },
  },
  {type: 'run.start', request_id: 's', launch: RECORD},
  {
    type: 'paths.complete',
    request_id: 'p',
    directory: '/home/me',
    entries: [
      {
        name: 'src',
        path: '/home/me/src',
        vibesys_checkout: false,
        vibesys_project: true,
        git_repository: true,
      },
    ],
    truncated: false,
  },
  {
    type: 'error',
    request_id: null,
    code: 'incompatible',
    message: 'no',
    path: null,
    supported_versions: [1, 1],
    vibesys_version: '0.1',
  },
];

type Json = null | boolean | number | string | Json[] | {[key: string]: Json};

/** Every copy of `value` with one unknown field added to one of its objects. */
function withUnknownField(value: Json): Json[] {
  if (Array.isArray(value)) {
    return value.flatMap((item, index) =>
      withUnknownField(item).map(changed =>
        value.map((other, at) => (at === index ? changed : other)),
      ),
    );
  }
  if (value === null || typeof value !== 'object') return [];
  const here: Json = {...value, unexpected_field: 1};
  const nested = Object.entries(value).flatMap(([key, child]) =>
    withUnknownField(child).map(changed => ({...value, [key]: changed})),
  );
  return [here, ...nested];
}

function rejects(line: string, parse: (line: string) => unknown): BackendClientError {
  try {
    parse(line);
  } catch (error) {
    expect(error).toBeInstanceOf(BackendClientError);
    return error as BackendClientError;
  }
  throw new Error(`accepted ${line}`);
}

describe('parseHelperMessage', () => {
  it('accepts every complete message as the helper writes it', () => {
    for (const message of VALID)
      expect(parseHelperMessage(JSON.stringify(message))).toEqual(message);
  });

  it('rejects an unknown field at any depth, naming it', () => {
    for (const message of VALID) {
      for (const changed of withUnknownField(message as Json)) {
        expect(rejects(JSON.stringify(changed), parseHelperMessage).message).toContain(
          'unexpected_field',
        );
      }
    }
  });

  it('rejects an unknown message type, closed-set member, and out-of-range version', () => {
    rejects(JSON.stringify({type: 'instances.moved', watch_id: 'w'}), parseHelperMessage);
    const [hello] = VALID;
    rejects(JSON.stringify({...hello, features: ['teleport']}), parseHelperMessage);
    rejects(JSON.stringify({...hello, protocol_version: 0}), parseHelperMessage);
    rejects('{"type": "ping"', parseHelperMessage);
  });

  it('reads a relayed channel end and rejects an unknown outcome', () => {
    const end = {outcome: 'run_gone', exit_status: 4, detail: 'gone'};
    expect(parseChannelEnd(JSON.stringify(end))).toEqual(end);
    rejects(JSON.stringify({...end, outcome: 'vanished'}), parseChannelEnd);
  });
});
