import {describe, it} from 'node:test';
import {BackendClientError} from './errors.js';
import {
  helperFeaturesForRequest,
  parseChannelEnd,
  parseHelperMessage,
  parseRunStartCompensationFailure,
} from './host-helper.js';
import {expect} from './test-support/expect.js';

const RECORD = {
  version: 5,
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
      version: 5,
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
    type: 'harnesses.list',
    request_id: 'l',
    harnesses: [
      {provider: 'codex', model: 'gpt-a', default: true},
      {provider: 'codex', model: 'gpt-b', default: false},
    ],
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

  it('gates a request on its method and on each gated field it sets', () => {
    const start = {type: 'run.start', project: '/p'};
    const unset = {...start, harness: null};
    const chosen = {...start, harness: {provider: 'codex', model: 'gpt-a'}};
    expect(helperFeaturesForRequest(start)).toEqual([]);
    expect(helperFeaturesForRequest(unset)).toEqual([]);
    expect(helperFeaturesForRequest(chosen)).toEqual(['harnesses']);
    expect(helperFeaturesForRequest({type: 'harnesses.list'})).toEqual(['harnesses']);
    const generations = {type: 'harnesses.list', with_generations: true};
    expect(helperFeaturesForRequest(generations)).toEqual(['harnesses', 'harness_generations']);
    const agents = {'multi-judge': {provider: 'codex', model: 'gpt-a'}};
    const withAgents = {...start, agents};
    const withNone = {...start, agents: null};
    expect(helperFeaturesForRequest(withAgents)).toEqual(['run_role_agents']);
    expect(helperFeaturesForRequest(withNone)).toEqual([]);
    const preflight = {type: 'run.preflight', project: '/p', harness: chosen.harness, agents};
    expect(helperFeaturesForRequest(preflight)).toEqual([
      'preflight',
      'harnesses',
      'run_role_agents',
    ]);
    const measured = {...start, measure_on: {kind: 'slurm', cluster: 'lab'}};
    expect(helperFeaturesForRequest(measured)).toEqual(['measurement_target']);
    const unmeasured = {...start, measure_on: null};
    expect(helperFeaturesForRequest(unmeasured)).toEqual([]);
    expect(helperFeaturesForRequest({type: 'compute.list'})).toEqual(['machine_compute']);
    expect(helperFeaturesForRequest({type: 'compute.remove_cluster'})).toEqual(['machine_compute']);
    expect(helperFeaturesForRequest({type: 'agent_logins.list'})).toEqual(['agent_logins']);
    expect(helperFeaturesForRequest({type: 'agent_logins.start'})).toEqual(['agent_logins']);
    expect(helperFeaturesForRequest({type: 'ping'})).toEqual([]);
  });

  it('reads a relayed channel end and rejects an unknown outcome', () => {
    const end = {outcome: 'run_gone', exit_status: 4, detail: 'gone'};
    expect(parseChannelEnd(JSON.stringify(end))).toEqual(end);
    rejects(JSON.stringify({...end, outcome: 'vanished'}), parseChannelEnd);
  });

  it('reads a failed start compensation report and rejects unknown evidence', () => {
    const failure = {
      type: 'run.start_compensation_failed',
      exit_status: 12,
      request_id: 's',
      kind: 'stop_unconfirmed',
      detail: 'compensating stop returned still_running',
      stop_result: {
        version: 1,
        id: '0123456789ab',
        outcome: 'still_running',
        route: 'signal',
      },
    };
    expect(parseRunStartCompensationFailure(JSON.stringify(failure))).toEqual(failure);
    rejects(JSON.stringify({...failure, kind: 'forgotten'}), parseRunStartCompensationFailure);
    rejects(
      JSON.stringify({...failure, instance_id: 'ffffffffffff'}),
      parseRunStartCompensationFailure,
    );
    const withoutResult = {
      type: failure.type,
      exit_status: failure.exit_status,
      request_id: failure.request_id,
      kind: failure.kind,
      detail: failure.detail,
    };
    rejects(JSON.stringify(withoutResult), parseRunStartCompensationFailure);
    rejects(
      JSON.stringify({...failure, stop_result: {...failure.stop_result, outcome: 'stopped'}}),
      parseRunStartCompensationFailure,
    );
    rejects(
      JSON.stringify({...failure, stop_result: {...failure.stop_result, outcome: 'not_running'}}),
      parseRunStartCompensationFailure,
    );

    const failed = {
      type: 'run.start_compensation_failed',
      exit_status: 12,
      request_id: 's',
      instance_id: '0123456789ab',
      kind: 'stop_failed',
      detail: 'OSError: stop failed',
    };
    expect(parseRunStartCompensationFailure(JSON.stringify(failed))).toEqual(failed);
    rejects(
      JSON.stringify({...failed, stop_result: failure.stop_result}),
      parseRunStartCompensationFailure,
    );
  });
});
