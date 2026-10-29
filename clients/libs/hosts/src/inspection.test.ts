import {describe, expect, test} from 'bun:test';
import type {InstanceInspection, LiveInstanceRecord} from '@vibesys/contracts/host-helper';
import {inspectionReply} from './inspection.js';

const ID = '0123456789ab';

function record(id = ID): LiveInstanceRecord {
  return {
    version: 5,
    id,
    status: 'serving',
    phase: {status: 'running', stage: null, progress: null, reason: null},
    socket_path: '/run/control.sock',
    log_path: '/run/server.log',
    project_root: '/project',
    run_id: 'run-1',
    pid: 42,
    started_at: 1,
    ended_at: null,
    hostname: 'host',
    protocol_version: 1,
    vibesys_version: '0+test',
    vibesys_root: '/src/vibesys',
  };
}

function inspection(overrides: Partial<InstanceInspection> = {}): InstanceInspection {
  return {
    version: 1,
    id: ID,
    state: 'live',
    record: record(),
    metadata: {
      version: 1,
      id: ID,
      project_root: '/project',
      run_id: 'run-1',
      task: 'latency',
      pid: 42,
      started_at: 1,
      hostname: 'host',
      protocol_version: 1,
      vibesys_version: '0+test',
      vibesys_root: '/src/vibesys',
      control_socket_path: '/run/control.sock',
      server_log_path: '/run/server.log',
    },
    control_socket_path: '/run/control.sock',
    server_log_path: '/run/server.log',
    log: null,
    ...overrides,
  };
}

function problem(value: InstanceInspection, requested = ID): string {
  try {
    inspectionReply(requested, value);
  } catch (error) {
    return error instanceof Error ? error.message : String(error);
  }
  return '';
}

describe('inspectionReply', () => {
  test('accepts one coherent parsed helper observation unchanged', () => {
    const value = inspection();
    expect(inspectionReply(ID, value)).toBe(value);
  });

  test('rejects disagreement between the request, envelope, record, or metadata identities', () => {
    expect(problem(inspection(), 'ba9876543210')).toContain('requested ba9876543210');
    expect(problem(inspection({record: record('ba9876543210')}))).toContain('record id');
    const current = inspection();
    expect(
      problem(
        inspection({
          metadata: current.metadata === null ? null : {...current.metadata, id: 'ba9876543210'},
        }),
      ),
    ).toContain('metadata id');
    expect(
      problem(
        inspection({
          record: {...record(), run_id: 'another-run'},
        }),
      ),
    ).toContain('name different runs');
  });

  test('requires a live observation to carry its record', () => {
    expect(problem(inspection({record: null}))).toContain('live inspection requires a record');
  });

  test('rejects relative paths and retained paths that disagree with the envelope', () => {
    expect(problem(inspection({control_socket_path: 'run/control.sock'}))).toContain(
      'control_socket_path must be an absolute',
    );
    expect(problem(inspection({record: {...record(), project_root: 'project'}}))).toContain(
      'record.project_root must be an absolute',
    );
    const current = inspection();
    expect(
      problem(
        inspection({
          metadata:
            current.metadata === null
              ? null
              : {...current.metadata, server_log_path: '/other/server.log'},
        }),
      ),
    ).toContain('top-level paths do not match');
  });

  test('refuses a server log tail the app never asks for', () => {
    expect(
      problem(inspection({log: {state: 'available', text: 'tail', truncated: false}})),
    ).toContain('server log the app did not ask for');
    expect(problem(inspection({log: {state: 'missing'}}))).toContain(
      'server log the app did not ask for',
    );
  });
});
