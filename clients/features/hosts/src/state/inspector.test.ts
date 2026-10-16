import {describe, expect, test} from 'bun:test';
import type {InstanceInspection, LiveInstanceRecord} from '@vibesys/contracts/host-helper';
import type {WelcomeInspection, WelcomeResult} from '@vibesys/ipc';
import {checkMachine} from '@vibesys/machine/testing';
import {createActor, fromPromise} from 'xstate';
import {
  type InspectorCopy,
  type InspectorInspect,
  type InspectorTarget,
  inspectionFields,
  inspectorMachine,
  inspectorView,
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
    version: 5,
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

class Deferred<T> {
  readonly promise: Promise<T>;
  #resolve: (value: T) => void = () => undefined;

  constructor() {
    this.promise = new Promise(resolve => {
      this.#resolve = resolve;
    });
  }

  resolve(value: T): void {
    this.#resolve(value);
  }
}

function failure<T>(error: string): WelcomeResult<T> {
  return {ok: false, error, authNeeded: false};
}

/** Let promise actors report without consulting timers or wall-clock time. */
async function flush(): Promise<void> {
  for (let turn = 0; turn < 10; turn += 1) await Promise.resolve();
}

function inspectorActor(
  inspect: (request: InspectorInspect) => Promise<WelcomeResult<WelcomeInspection>>,
  copy: (request: InspectorCopy) => Promise<WelcomeResult<null>> = () =>
    Promise.resolve({ok: true, value: null}),
) {
  return createActor(
    inspectorMachine.provide({
      actors: {
        inspect: fromPromise<WelcomeResult<WelcomeInspection>, InspectorInspect>(({input}) =>
          inspect(input),
        ),
        copy: fromPromise<WelcomeResult<null>, InspectorCopy>(({input}) => copy(input)),
      },
    }),
  ).start();
}

describe('inspector machine', () => {
  test('model: every operation belongs to the state that owns it', () => {
    checkMachine({
      machine: inspectorMachine,
      input: undefined,
      event: (random, live) => {
        const outcomes = live.operations.flatMap(operation =>
          operation === 'inspect'
            ? [
                live.done(operation, {ok: true, value: inspection()}),
                live.done(operation, failure<WelcomeInspection>('inspection unavailable')),
                live.error(operation, new Error('inspection transport failed')),
              ]
            : [
                live.done(operation, {ok: true, value: null}),
                live.done(operation, failure<null>('clipboard unavailable')),
                live.error(operation, new Error('clipboard failed')),
              ],
        );
        return random.pick([
          {type: 'open', target},
          {type: 'close'},
          {type: 'refresh'},
          {type: 'copy', label: 'Project', value: '/project'},
          ...outcomes,
        ]);
      },
      invariants: snapshot => {
        const view = inspectorView(snapshot);
        const problems: string[] = [];
        if ((view.target === null) !== snapshot.matches('closed')) {
          problems.push('only the closed state may have no target');
        }
        if ((view.load.kind === 'loading') !== snapshot.matches('loading')) {
          problems.push('loading view and state disagree');
        }
        if ((view.copying !== null) !== snapshot.matches('copying')) {
          problems.push('copying view and state disagree');
        }
        return problems;
      },
    });
  });

  test('only the latest inspection can replace the view', async () => {
    const replies: Array<Deferred<WelcomeResult<WelcomeInspection>>> = [];
    const actor = inspectorActor(() => {
      const reply = new Deferred<WelcomeResult<WelcomeInspection>>();
      replies.push(reply);
      return reply.promise;
    });

    actor.send({type: 'open', target});
    expect(replies).toHaveLength(1);
    actor.send({type: 'refresh'});
    expect(replies).toHaveLength(2);
    expect(inspectorView(actor.getSnapshot())).toMatchObject({load: {kind: 'loading'}});

    replies[0]?.resolve({ok: true, value: inspection()});
    await flush();
    expect(inspectorView(actor.getSnapshot()).load.kind).toBe('loading');

    replies[1]?.resolve({ok: true, value: inspection({state: 'ended'})});
    await flush();
    expect(inspectorView(actor.getSnapshot()).load).toMatchObject({
      kind: 'ready',
      inspection: {state: 'ended'},
    });
  });

  test('closing stops the inspection and a late answer cannot reopen the sheet', async () => {
    const reply = new Deferred<WelcomeResult<WelcomeInspection>>();
    const actor = inspectorActor(() => reply.promise);

    actor.send({type: 'open', target});
    actor.send({type: 'close'});
    const closed = inspectorView(actor.getSnapshot());
    expect(closed).toMatchObject({target: null, load: {kind: 'idle'}});

    reply.resolve({ok: true, value: inspection()});
    await flush();
    expect(inspectorView(actor.getSnapshot())).toBe(closed);
  });

  test('refresh cancels copy state and fences its late failure behind the new load', async () => {
    const copyReply = new Deferred<WelcomeResult<null>>();
    let inspections = 0;
    const actor = inspectorActor(
      () =>
        Promise.resolve(
          inspections++ === 0
            ? {ok: true, value: inspection()}
            : failure<WelcomeInspection>('inspection unavailable'),
        ),
      () => copyReply.promise,
    );

    actor.send({type: 'open', target});
    await flush();
    actor.send({type: 'copy', label: 'Project', value: '/project'});
    expect(inspectorView(actor.getSnapshot()).copying).not.toBeNull();

    actor.send({type: 'refresh'});
    await flush();
    const failed = inspectorView(actor.getSnapshot());
    expect(failed).toMatchObject({
      load: {kind: 'error', message: 'inspection unavailable'},
      copying: null,
      notice: null,
    });

    copyReply.resolve(failure('clipboard unavailable'));
    await flush();
    expect(inspectorView(actor.getSnapshot())).toBe(failed);
  });

  test('a completed copy reports its label', async () => {
    const actor = inspectorActor(() => Promise.resolve({ok: true, value: inspection()}));
    actor.send({type: 'open', target});
    await flush();

    actor.send({type: 'copy', label: 'Project', value: '/project'});
    await flush();

    expect(inspectorView(actor.getSnapshot()).notice).toEqual({
      text: 'Project copied.',
      error: false,
    });
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
      inspectionFields(target, {
        ...current,
        record: current.record === null ? null : {...current.record, started_at: upperSeconds},
        metadata:
          current.metadata === null ? null : {...current.metadata, started_at: upperSeconds},
      }).map(field => [field.label, field.value]),
    );

    expect(fields['Started']).toBe('+275760-09-13T00:00:00.000Z');
  });
});
