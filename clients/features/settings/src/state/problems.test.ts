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

test('a connection test that failed is a problem until the settings change and it must be run again', () => {
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
  walk.do({
    type: 'tested',
    token: walk.last('test').token,
    host: HOST,
    result: {ok: true, value: {outcome: 'failed', stage: 'workspace', detail: 'not writable'}},
  });
  expect(walk.problems()).toEqual([
    {
      id: `settings/cluster-test/${HOST}`,
      tone: 'err',
      title: 'The workspace is not usable',
      detail: 'not writable',
    },
  ]);
  walk.do({type: 'field', host: HOST, key: 'workspace', value: '/other'});
  expect(walk.problems()).toEqual([]);
});

test('a cluster that was not saved is a problem when it names no field, until the next save', () => {
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
  walk.do({
    type: 'tested',
    token: walk.last('test').token,
    host: HOST,
    result: {ok: true, value: {outcome: 'passed', hardware: {}, partitions: []}},
  });
  walk.do({type: 'save', host: HOST});
  walk.do({
    type: 'saved',
    token: walk.last('save').token,
    host: HOST,
    result: {ok: false, error: 'the file is read-only'},
  });
  expect(walk.problems().map(each => each.title)).toEqual(['The cluster was not saved.']);
  walk.do({type: 'save', host: HOST});
  expect(walk.problems()).toEqual([]);
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
