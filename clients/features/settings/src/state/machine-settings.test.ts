import {describe, expect, test} from 'bun:test';
import type {AgentLogin, ComputeSettings} from '@vibesys/ipc';
import {Seeded} from '@vibesys/store/testing';
import {
  type ClusterFields,
  canSave,
  INITIAL_MACHINE_SETTINGS,
  type MachineSettingsAction,
  type MachineSettingsCall,
  type MachineSettingsState,
  machineSettingsInvariants,
  machineSettingsStep,
  shownMachine,
  stale,
} from './index.js';

const HOST = 'ssh:host-b';
const COMPUTE: ComputeSettings = {
  config_path: '/home/me/.config/vibesys/compute.toml',
  clusters: [],
  modal: {token_present: false},
};
const LOGIN: AgentLogin = {
  provider: 'claude',
  provider_name: 'Claude Code',
  installed: true,
  state: 'signed_out',
  api_key_variables: [],
  detail: '',
  can_start_login: true,
};

/** A walk of actions through the reducer, keeping every request it made. */
class Walk {
  state: MachineSettingsState = INITIAL_MACHINE_SETTINGS;
  readonly issued: MachineSettingsCall[] = [];

  do(action: MachineSettingsAction): MachineSettingsCall[] {
    const step = machineSettingsStep(this.state, action);
    this.state = step.state;
    this.issued.push(...step.requests);
    return [...step.requests];
  }

  last(kind: MachineSettingsCall['kind']): MachineSettingsCall {
    const found = this.issued.filter(call => call.kind === kind).at(-1);
    if (found === undefined) throw new Error(`no ${kind} request was made`);
    return found;
  }

  type(fields: Partial<ClusterFields>): void {
    for (const [key, value] of Object.entries(fields)) {
      this.do({type: 'field', host: HOST, key: key as keyof ClusterFields, value});
    }
  }

  /** A form of a reachable cluster, tested and passed. */
  passing(): void {
    this.do({type: 'focus', host: HOST, section: 'clusters'});
    this.do({type: 'open-form', host: HOST});
    this.type({name: 'lab', host: 'lab-login', workspace: '/scratch/vibesys'});
    this.do({type: 'test', host: HOST});
    const test = this.last('test');
    this.do({
      type: 'tested',
      token: test.token,
      host: HOST,
      result: {
        ok: true,
        value: {
          outcome: 'passed',
          hardware: {accelerator: 'cuda', gpu_models: ['nvidia_h100'], gpus_per_node: 8},
          partitions: ['gpu'],
        },
      },
    });
  }

  form() {
    const form = this.state.views[HOST]?.form;
    if (form === null || form === undefined) throw new Error('no form is open');
    return form;
  }
}

describe('reading a machine', () => {
  test('showing a machine reads its clusters and its logins once, however often it is shown', () => {
    const walk = new Walk();
    expect(
      walk.do({type: 'focus', host: HOST, section: 'clusters'}).map(call => call.kind),
    ).toEqual(['compute', 'logins']);
    expect(walk.do({type: 'focus', host: HOST, section: 'logins'})).toEqual([]);
  });

  test('a machine section reads the machine that is shown: on landing, on choosing, on the list', () => {
    const walk = new Walk();
    expect(walk.do({type: 'hosts', hosts: ['local', HOST]})).toEqual([]);
    expect(walk.do({type: 'section', section: 'logins'}).map(call => call.host)).toEqual([
      'local',
      'local',
    ]);
    expect(walk.do({type: 'machine', host: HOST}).map(call => call.kind)).toEqual([
      'compute',
      'logins',
    ]);
    expect(walk.do({type: 'machine', host: HOST})).toEqual([]);
    expect(walk.do({type: 'section', section: 'appearance'})).toEqual([]);
  });

  test('opening Settings for a machine and section lands there and reads the machine', () => {
    const walk = new Walk();
    const requests = walk.do({type: 'focus', host: HOST, section: 'clusters'});
    expect(walk.state).toMatchObject({machine: HOST, section: 'clusters'});
    expect(requests.map(call => call.kind)).toEqual(['compute', 'logins']);
  });

  test('a reply for a reading since replaced is dropped', () => {
    const walk = new Walk();
    walk.do({type: 'focus', host: HOST, section: 'clusters'});
    const old = walk.last('compute');
    walk.do({type: 'refresh', host: HOST, what: 'compute'});
    walk.do({
      type: 'computed',
      token: old.token,
      host: HOST,
      result: {ok: true, value: {kind: 'offered', value: COMPUTE}},
    });
    expect(walk.state.views[HOST]?.compute.kind).toBe('loading');
    walk.do({
      type: 'computed',
      token: walk.last('compute').token,
      host: HOST,
      result: {ok: true, value: {kind: 'unsupported'}},
    });
    expect(walk.state.views[HOST]?.compute).toEqual({
      kind: 'ready',
      offered: {kind: 'unsupported'},
    });
  });

  test('the shown machine is the chosen one while it is listed, else the first listed', () => {
    const state = (machine: string | null, hosts: string[]) => ({
      ...INITIAL_MACHINE_SETTINGS,
      machine,
      hosts,
    });
    expect(shownMachine(state(null, ['local', HOST]))).toBe('local');
    expect(shownMachine(state(HOST, ['local', HOST]))).toBe(HOST);
    expect(shownMachine(state('ssh:gone', ['local', HOST]))).toBe('local');
    expect(shownMachine(state(HOST, []))).toBe(HOST);
    expect(shownMachine(state(null, []))).toBeNull();
  });
});

describe('adding a cluster', () => {
  test('a cluster is saved only after the test of exactly that cluster passed', () => {
    const walk = new Walk();
    walk.do({type: 'focus', host: HOST, section: 'clusters'});
    walk.do({type: 'open-form', host: HOST});
    walk.type({name: 'lab', host: 'lab-login', workspace: '/scratch/vibesys'});
    expect(canSave(walk.form())).toBe(false);
    expect(walk.do({type: 'save', host: HOST})).toEqual([]);
    walk.do({type: 'test', host: HOST});
    expect(walk.do({type: 'save', host: HOST})).toEqual([]);
    walk.do({
      type: 'tested',
      token: walk.last('test').token,
      host: HOST,
      result: {ok: true, value: {outcome: 'passed', hardware: {}, partitions: []}},
    });
    expect(canSave(walk.form())).toBe(true);
    walk.type({workspace: '/elsewhere'});
    expect(canSave(walk.form())).toBe(false);
    expect(stale(walk.form())).toBe(true);
    expect(walk.do({type: 'save', host: HOST})).toEqual([]);
  });

  test('a passing test fills the hardware, unless the user already stated it', () => {
    const filled = new Walk();
    filled.passing();
    expect(filled.form().fields).toMatchObject({
      accelerator: 'cuda',
      gpuModels: 'nvidia_h100',
      gpusPerNode: '8',
    });
    expect(canSave(filled.form())).toBe(true);

    const stated = new Walk();
    stated.do({type: 'focus', host: HOST, section: 'clusters'});
    stated.do({type: 'open-form', host: HOST});
    stated.type({name: 'lab', host: 'h', workspace: '/w', accelerator: 'rocm'});
    stated.do({type: 'test', host: HOST});
    stated.do({
      type: 'tested',
      token: stated.last('test').token,
      host: HOST,
      result: {
        ok: true,
        value: {outcome: 'passed', hardware: {accelerator: 'cuda'}, partitions: []},
      },
    });
    expect(stated.form().fields.accelerator).toBe('rocm');
  });

  test('a failed test keeps its stage and detail and cannot be saved', () => {
    const walk = new Walk();
    walk.do({type: 'focus', host: HOST, section: 'clusters'});
    walk.do({type: 'open-form', host: HOST});
    walk.type({name: 'lab', host: 'lab-login', workspace: '/w'});
    walk.do({type: 'test', host: HOST});
    walk.do({
      type: 'tested',
      token: walk.last('test').token,
      host: HOST,
      result: {ok: true, value: {outcome: 'failed', stage: 'workspace', detail: 'not writable'}},
    });
    expect(walk.form().test).toMatchObject({kind: 'failed', stage: 'workspace'});
    expect(canSave(walk.form())).toBe(false);
  });

  test('an incomplete form is not sent, and says which field it needs', () => {
    const cases: readonly [Partial<ClusterFields>, string][] = [
      [{}, 'name'],
      [{name: 'lab'}, 'host'],
      [{name: 'lab', host: 'h'}, 'workspace'],
      [{name: 'lab', host: 'h', workspace: '/w', nodes: '0'}, 'nodes'],
      [{name: 'lab', host: 'h', workspace: '/w', nodes: '1.5'}, 'nodes'],
      [{name: 'lab', host: 'h', workspace: '/w', accelerator: 'cpu', gpusPerNode: '2'}, 'hardware'],
      [{name: 'lab', host: 'h', workspace: '/w', gpusPerNode: 'many'}, 'hardware'],
    ];
    for (const [fields, field] of cases) {
      const walk = new Walk();
      walk.do({type: 'focus', host: HOST, section: 'clusters'});
      walk.do({type: 'open-form', host: HOST});
      walk.type(fields);
      expect(walk.do({type: 'test', host: HOST})).toEqual([]);
      expect(walk.form().problem?.field).toBe(field as never);
    }
  });

  test('a login-node cluster needs no host, and the options are sent as written', () => {
    const walk = new Walk();
    walk.do({type: 'focus', host: HOST, section: 'clusters'});
    walk.do({type: 'open-form', host: HOST});
    walk.type({
      name: 'campus',
      reach: 'login_node',
      workspace: '/work',
      sbatch: '--account=lab  --gres=gpu:8',
      partition: 'gpu',
      nodes: '2',
      timeLimit: '4:00:00',
    });
    walk.do({type: 'test', host: HOST});
    expect(walk.last('test')).toMatchObject({
      cluster: {
        name: 'campus',
        reach: {kind: 'login_node'},
        workspace: '/work',
        partition: 'gpu',
        nodes: 2,
        time_limit: '4:00:00',
        sbatch_options: ['--account=lab', '--gres=gpu:8'],
      },
    });
  });

  test('saving closes the form on the settings the machine answers; a refusal names its field', () => {
    const saved = new Walk();
    saved.passing();
    saved.do({type: 'save', host: HOST});
    expect(saved.form().saving).not.toBeNull();
    saved.do({
      type: 'saved',
      token: saved.last('save').token,
      host: HOST,
      result: {ok: true, value: COMPUTE},
    });
    expect(saved.state.views[HOST]?.form).toBeNull();
    expect(saved.state.views[HOST]?.compute).toEqual({
      kind: 'ready',
      offered: {kind: 'offered', value: COMPUTE},
    });

    const refused = new Walk();
    refused.passing();
    refused.do({type: 'save', host: HOST});
    refused.do({
      type: 'saved',
      token: refused.last('save').token,
      host: HOST,
      result: {ok: false, error: "a cluster named 'lab' is already set up"},
    });
    expect(refused.form().saving).toBeNull();
    expect(refused.form().problem?.field).toBe('name');
  });

  test('the form cannot be closed or edited while its save is awaited', () => {
    const walk = new Walk();
    walk.passing();
    walk.do({type: 'save', host: HOST});
    walk.do({type: 'close-form', host: HOST});
    walk.type({name: 'other'});
    expect(walk.form().fields.name).toBe('lab');
  });
});

describe('removing a cluster and signing in', () => {
  test('a removal is answered by the settings after it, once, for the name asked', () => {
    const walk = new Walk();
    walk.do({type: 'focus', host: HOST, section: 'clusters'});
    walk.do({type: 'remove', host: HOST, name: 'lab'});
    expect(walk.do({type: 'remove', host: HOST, name: 'other'})).toEqual([]);
    walk.do({
      type: 'removed',
      token: walk.last('remove').token + 1,
      host: HOST,
      result: {ok: true, value: COMPUTE},
    });
    expect(walk.state.views[HOST]?.removing).not.toBeNull();
    walk.do({
      type: 'removed',
      token: walk.last('remove').token,
      host: HOST,
      result: {ok: false, error: 'read only'},
    });
    expect(walk.state.views[HOST]).toMatchObject({removing: null, removeProblem: 'read only'});
  });

  test('starting a login shows what to do next, and reading the logins again clears it', () => {
    const walk = new Walk();
    walk.do({type: 'focus', host: HOST, section: 'clusters'});
    walk.do({
      type: 'logins-read',
      token: walk.last('logins').token,
      host: HOST,
      result: {ok: true, value: {kind: 'offered', value: [LOGIN]}},
    });
    walk.do({type: 'sign-in', host: HOST, provider: 'claude'});
    expect(walk.do({type: 'sign-in', host: HOST, provider: 'claude'})).toEqual([]);
    walk.do({
      type: 'signed-in',
      token: walk.last('start-login').token,
      host: HOST,
      provider: 'claude',
      result: {ok: true, value: {outcome: 'started', instruction: 'Open the link'}},
    });
    expect(walk.state.views[HOST]?.starts['claude']).toEqual({
      kind: 'started',
      instruction: 'Open the link',
    });
    walk.do({type: 'refresh', host: HOST, what: 'logins'});
    walk.do({
      type: 'logins-read',
      token: walk.last('logins').token,
      host: HOST,
      result: {ok: true, value: {kind: 'offered', value: [{...LOGIN, state: 'signed_in'}]}},
    });
    expect(walk.state.views[HOST]?.starts).toEqual({});
  });
});

/** What one random action may draw on: a machine, a token of an earlier request, and a verdict. */
interface Draw {
  readonly host: string;
  readonly token: number;
  readonly good: boolean;
  readonly pick: <T>(values: readonly T[]) => T;
}

const FAILED = {ok: false as const, error: 'cluster.name: no'};

/** One way to make an action: a user's, or a reply to any request ever made, stale or not. */
const MAKERS: readonly ((draw: Draw) => MachineSettingsAction)[] = [
  ({pick}) => ({type: 'hosts', hosts: pick([[], ['local'], ['local', HOST]])}),
  ({pick}) => ({type: 'section', section: pick(['clusters', 'logins', 'appearance'] as const)}),
  ({host}) => ({type: 'machine', host}),
  ({host, pick}) => ({type: 'refresh', host, what: pick(['compute', 'logins'] as const)}),
  ({host, pick}) => ({type: 'focus', host, section: pick(['clusters', 'logins'] as const)}),
  ({host}) => ({type: 'open-form', host}),
  ({host}) => ({type: 'close-form', host}),
  ({host, pick}) => ({
    type: 'field',
    host,
    key: pick(['name', 'host', 'workspace'] as const),
    value: pick(['', 'x', '/w']),
  }),
  ({host, pick}) => ({type: 'field', host, key: 'accelerator', value: pick(['', 'cpu', 'cuda'])}),
  ({host}) => ({type: 'test', host}),
  ({host}) => ({type: 'save', host}),
  ({host}) => ({type: 'remove', host, name: 'lab'}),
  ({host}) => ({type: 'sign-in', host, provider: 'claude'}),
  ({host, token, good}) => ({
    type: 'computed',
    token,
    host,
    result: good ? {ok: true, value: {kind: 'offered', value: COMPUTE}} : FAILED,
  }),
  ({host, token, good}) => ({
    type: 'logins-read',
    token,
    host,
    result: good ? {ok: true, value: {kind: 'unsupported'}} : FAILED,
  }),
  ({host, token, good}) => ({
    type: 'tested',
    token,
    host,
    result: good ? {ok: true, value: {outcome: 'passed', hardware: {}, partitions: []}} : FAILED,
  }),
  ({host, token, good}) => ({
    type: 'saved',
    token,
    host,
    result: good ? {ok: true, value: COMPUTE} : FAILED,
  }),
  ({host, token, good}) => ({
    type: 'removed',
    token,
    host,
    result: good ? {ok: true, value: COMPUTE} : FAILED,
  }),
  ({host, token, good}) => ({
    type: 'signed-in',
    token,
    host,
    provider: 'claude',
    result: good ? {ok: true, value: {outcome: 'started'}} : FAILED,
  }),
];

describe('every state', () => {
  test('property: whatever the actions and replies, tokens were issued and a save passed its test', () => {
    for (const seed of [1, 2, 3, 4, 5]) {
      const random = new Seeded(seed);
      const walk = new Walk();
      for (let step = 0; step < 400; step += 1) {
        const request = walk.issued.length === 0 ? undefined : random.pick(walk.issued);
        const draw: Draw = {
          host: random.pick([HOST, 'local']),
          token: request === undefined ? 1 : random.pick([request.token, request.token + 1]),
          good: random.pick([true, false]),
          pick: values => random.pick(values),
        };
        walk.do(random.pick(MAKERS)(draw));
        expect(machineSettingsInvariants(walk.state)).toEqual([]);
      }
    }
  });
});
