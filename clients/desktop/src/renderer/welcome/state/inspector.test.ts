import {describe, expect, test} from 'bun:test';
import type {InstanceInspection, LiveInstanceRecord} from '@vibesys/backend-client/host-helper';
import {
  INSPECTOR_CLOSED,
  type InspectorTarget,
  inspectionFields,
  inspectorStep,
} from './inspector.js';

const target: InspectorTarget = {
  host: 'local',
  hostLabel: 'This Mac',
  instanceId: '0123456789ab',
  task: 'spsc',
  runId: 'run-7',
  project: '/project',
  focusFallbackId: 'recent-section',
};

function inspection(overrides: Partial<InstanceInspection> = {}): InstanceInspection {
  const record: LiveInstanceRecord = {
    version: 4,
    id: target.instanceId,
    status: 'serving',
    phase: {status: 'running', stage: null, progress: null, reason: null},
    socket_path: '/run/control.sock',
    log_path: '/run/server.log',
    project_root: '/project',
    run_id: 'run-8',
    pid: 4242,
    started_at: 1_700_000_000,
    ended_at: null,
    hostname: 'node-1',
    protocol_version: 1,
    vibesys_version: '0.0.0+fake',
    vibesys_root: '/src/vibesys',
  };
  return {
    version: 1,
    id: target.instanceId,
    state: 'live',
    record,
    metadata: {
      version: 1,
      id: target.instanceId,
      project_root: '/home/user/project',
      run_id: 'run-8',
      task: 'mpmc',
      pid: 4242,
      started_at: 1_700_000_000,
      hostname: 'node-1',
      protocol_version: 1,
      vibesys_version: '0.0.0+fake',
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

describe('inspector state', () => {
  test('opens by loading metadata and only the latest refresh can replace it', () => {
    let step = inspectorStep(INSPECTOR_CLOSED, {type: 'open', target});
    expect(step.requests).toEqual([
      {kind: 'inspect', token: 1, host: 'local', instance: target.instanceId, includeLog: false},
    ]);
    const first = step.state.load.kind === 'loading' ? step.state.load.token : 0;
    step = inspectorStep(step.state, {type: 'load-log'});
    const second = step.state.load.kind === 'loading' ? step.state.load.token : 0;

    expect(
      inspectorStep(step.state, {
        type: 'inspection-reply',
        token: first,
        result: {ok: true, value: inspection()},
      }).state,
    ).toBe(step.state);
    const answered = inspectorStep(step.state, {
      type: 'inspection-reply',
      token: second,
      result: {ok: true, value: inspection({state: 'ended'})},
    }).state;
    expect(answered.load).toMatchObject({kind: 'ready', inspection: {state: 'ended'}});
    expect(answered.includeLog).toBe(true);
  });

  test('closing invalidates an in-flight answer and clears log consent', () => {
    const loading = inspectorStep(INSPECTOR_CLOSED, {type: 'open', target}).state;
    const token = loading.load.kind === 'loading' ? loading.load.token : 0;
    const closed = inspectorStep(loading, {type: 'close'}).state;
    const late = inspectorStep(closed, {
      type: 'inspection-reply',
      token,
      result: {ok: true, value: inspection()},
    }).state;
    expect(late).toBe(closed);
    expect(closed.target).toBeNull();
    expect(closed.includeLog).toBe(false);
  });
});

describe('inspection fields', () => {
  test('separates the app host from the reported hostname and exposes safe copy values', () => {
    const fields = inspectionFields(target, inspection());
    expect(Object.fromEntries(fields.map(field => [field.label, field.value]))).toMatchObject({
      Host: 'This Mac',
      Hostname: 'node-1',
      Task: 'mpmc',
      Run: 'run-8',
      Instance: target.instanceId,
      Project: '/home/user/project',
      'VibeSys checkout': '/src/vibesys',
      'Control socket': '/run/control.sock',
      'Server log': '/run/server.log',
    });
  });

  test('an ended entry without retained metadata uses recent context', () => {
    const fields = Object.fromEntries(
      inspectionFields(target, inspection({state: 'ended', record: null, metadata: null})).map(
        field => [field.label, field.value],
      ),
    );
    expect(fields).toMatchObject({
      Host: 'This Mac',
      Hostname: 'Unknown',
      Task: 'spsc',
      Run: 'run-7',
      Project: '/project',
      PID: 'Unknown',
      State: 'ended',
    });
  });

  test('presents the upper contract timestamp without throwing', () => {
    const upperSeconds = 8_640_000_000_000;
    const current = inspection();
    const fields = Object.fromEntries(
      inspectionFields(
        target,
        {
          ...current,
          record: current.record === null ? null : {...current.record, started_at: upperSeconds},
          metadata:
            current.metadata === null ? null : {...current.metadata, started_at: upperSeconds},
        },
      ).map(field => [field.label, field.value]),
    );

    expect(fields['Started']).toBe('+275760-09-13T00:00:00.000Z');
  });
});
