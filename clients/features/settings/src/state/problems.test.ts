import {expect, test} from 'bun:test';
import type {AgentLogin} from '@vibesys/ipc';
import {
  INITIAL_MACHINE_SETTINGS,
  initialSettings,
  type MachineSettingsAction,
  type MachineSettingsCall,
  type MachineSettingsState,
  machineSettingsProblems,
  machineSettingsStep,
  settingsProblems,
  settingsStep,
} from './index.js';

const HOST = 'ssh:host-b';
const label = (host: string): string => host.replace('ssh:', '');
const LOGIN: AgentLogin = {
  provider: 'claude',
  provider_name: 'Claude Code',
  installed: true,
  state: 'signed_out',
  api_key_variables: [],
  detail: '',
  can_start_login: true,
};

class Walk {
  state: MachineSettingsState = INITIAL_MACHINE_SETTINGS;
  readonly issued: MachineSettingsCall[] = [];
  do(action: MachineSettingsAction): void {
    const step = machineSettingsStep(this.state, action);
    this.state = step.state;
    this.issued.push(...step.requests);
  }
  last(kind: MachineSettingsCall['kind']): MachineSettingsCall {
    const found = this.issued.filter(call => call.kind === kind).at(-1);
    if (found === undefined) throw new Error(`no ${kind} request was made`);
    return found;
  }
  problems() {
    return machineSettingsProblems(this.state, label);
  }
}

test('a save that failed is a problem until the next change', () => {
  const stored = {
    appearance: {mode: 'system', light: 'paper', dark: 'ink'},
    notifications: {enabled: true},
    archived: [],
  } as const;
  const first = initialSettings('light');
  const loaded = settingsStep(first.state, {
    type: 'loaded',
    token: first.requests[0]?.token ?? 0,
    preferences: stored,
  });
  expect(settingsProblems(loaded.state)).toEqual([]);
  const change = {kind: 'notifications', enabled: false} as const;
  const saving = settingsStep(loaded.state, {type: 'change', change});
  const failed = settingsStep(saving.state, {
    type: 'save-failed',
    token: saving.requests[0]?.token ?? 0,
    error: 'The disk is full.',
  });
  expect(settingsProblems(failed.state)).toEqual([
    {
      id: 'settings/save',
      tone: 'err',
      title: 'Could not save the change.',
      detail: 'The disk is full.',
    },
  ]);
  expect(settingsProblems(settingsStep(failed.state, {type: 'change', change}).state)).toEqual([]);
});

test('settings still loading report nothing', () => {
  expect(settingsProblems(initialSettings('light').state)).toEqual([]);
});

type TestAnswer = Extract<MachineSettingsAction, {readonly type: 'tested'}>['result'];
type SaveAnswer = Extract<MachineSettingsAction, {readonly type: 'saved'}>['result'];

/** Every kind of answer a connection test can get: it passed, failed at a stage, or errored. */
const TEST_ANSWERS: readonly TestAnswer[] = [
  {ok: true, value: {outcome: 'passed', hardware: {}, partitions: []}},
  {ok: true, value: {outcome: 'failed', stage: 'reach', detail: 'no route'}},
  {ok: true, value: {outcome: 'failed', stage: 'scheduler', detail: 'no sinfo'}},
  {ok: true, value: {outcome: 'failed', stage: 'workspace', detail: 'not writable'}},
  {ok: false, error: 'the helper did not answer'},
  {ok: false, error: 'cluster.workspace: not a directory'},
];

/** Every kind of answer a save can get: it worked, errored, or named a field. */
const SAVE_ANSWERS: readonly SaveAnswer[] = [
  {ok: true, value: {config_path: '/c.toml', clusters: [], modal: {token_present: false}}},
  {ok: false, error: 'the file is read-only'},
  {ok: false, error: 'cluster.name: already set up'},
];

/** A walk with the Add a cluster form filled in and its test answered with `answer`. */
function walkTested(answer: TestAnswer): Walk {
  const walk = new Walk();
  walk.do({type: 'focus', host: HOST, section: 'clusters'});
  walk.do({type: 'open-form', host: HOST});
  for (const [key, value] of [
    ['name', 'lab'],
    ['host', 'lab-login'],
    ['workspace', '/w'],
  ] as const) {
    walk.do({type: 'field', host: HOST, key, value});
  }
  walk.do({type: 'test', host: HOST});
  walk.do({type: 'tested', token: walk.last('test').token, host: HOST, result: answer});
  return walk;
}

test('the Add a cluster form holds its own failures: none is a problem on the board', () => {
  for (const answer of TEST_ANSWERS) {
    expect(walkTested(answer).problems()).toEqual([]);
  }
  for (const saved of SAVE_ANSWERS) {
    const walk = walkTested({ok: true, value: {outcome: 'passed', hardware: {}, partitions: []}});
    walk.do({type: 'save', host: HOST});
    walk.do({type: 'saved', token: walk.last('save').token, host: HOST, result: saved});
    expect(walk.problems()).toEqual([]);
  }
});

test('a login that did not start is a problem naming the machine, until the next try', () => {
  const walk = new Walk();
  walk.do({type: 'focus', host: HOST, section: 'logins'});
  walk.do({
    type: 'logins-read',
    token: walk.last('logins').token,
    host: HOST,
    result: {ok: true, value: {kind: 'offered', value: [LOGIN]}},
  });
  walk.do({type: 'sign-in', host: HOST, provider: 'claude'});
  walk.do({
    type: 'signed-in',
    token: walk.last('start-login').token,
    host: HOST,
    provider: 'claude',
    result: {ok: false, error: 'The helper did not answer.'},
  });
  expect(walk.problems()).toEqual([
    {
      id: `settings/login-start/${HOST}/claude`,
      tone: 'err',
      title: 'Could not start the login on host-b.',
      detail: 'The helper did not answer.',
    },
  ]);
  walk.do({type: 'sign-in', host: HOST, provider: 'claude'});
  expect(walk.problems()).toEqual([]);
});
