import {describe, expect, test} from 'bun:test';
import type {AgentLogin} from '@vibesys/ipc';
import {
  hardwareModels,
  hardwareWords,
  loginWords,
  modalWords,
  reachWords,
  signInBlocked,
} from './index.js';

const LOGIN: AgentLogin = {
  provider: 'claude',
  provider_name: 'Claude Code',
  installed: true,
  state: 'signed_in',
  api_key_variables: ['ANTHROPIC_API_KEY'],
  detail: '',
  can_start_login: true,
};

describe('machine words', () => {
  test('a cluster says how it is reached from the machine that is shown', () => {
    const base = {name: 'c', workspace: '/w', sbatch_options: []};
    expect(reachWords({...base, reach: {kind: 'ssh', host: 'lab-login'}}, 'host-b')).toBe(
      'Over SSH, host alias lab-login',
    );
    expect(reachWords({...base, reach: {kind: 'login_node'}}, 'host-b')).toBe(
      'host-b is a login node of this cluster',
    );
  });

  test('hardware names what is stated and says so when nothing is', () => {
    expect(hardwareWords(null)).toBe('Not stated');
    expect(hardwareWords({})).toBe('Not stated');
    const h100 = {accelerator: 'cuda' as const, gpu_models: ['nvidia_h100'], gpus_per_node: 8};
    expect(hardwareWords(h100)).toBe('8 GPUs per node · NVIDIA (CUDA)');
    expect(hardwareModels(h100)).toEqual(['nvidia_h100']);
    expect(hardwareWords({accelerator: 'cpu'})).toBe('CPU only');
  });

  test('a Modal token is present or not, and says where it is held, never what it is', () => {
    expect(modalWords({token_present: false})).toMatchObject({label: 'No token', tone: 'warn'});
    expect(modalWords({token_present: true, source: 'profile'}).detail).toContain('profile');
    expect(modalWords({token_present: true, source: 'environment'}).detail).toContain(
      'environment',
    );
  });

  test('a login is signed in, not signed in, cannot be told, or not installed, and says how', () => {
    expect(loginWords({...LOGIN, how: {kind: 'account'}})).toEqual({
      label: 'Signed in',
      tone: 'ok',
      how: {text: 'With your account', identifier: null},
    });
    expect(
      loginWords({...LOGIN, how: {kind: 'api_key', variable: 'ANTHROPIC_API_KEY'}}).how,
    ).toEqual({text: 'With the API key in', identifier: 'ANTHROPIC_API_KEY'});
    expect(loginWords({...LOGIN, how: {kind: 'api_key', file: '.gemini/creds.json'}}).how).toEqual({
      text: 'With the credentials in',
      identifier: '.gemini/creds.json',
    });
    expect(loginWords({...LOGIN, state: 'signed_out'})).toMatchObject({label: 'Not signed in'});
    expect(loginWords({...LOGIN, state: 'unknown'})).toMatchObject({label: 'Cannot tell'});
    expect(loginWords({...LOGIN, installed: false, state: 'unknown'})).toEqual({
      label: 'Not installed',
      tone: 'neutral',
      how: {text: '', identifier: null},
    });
  });

  test('Sign in is blocked, with a reason, unless the harness is installed and has a login command', () => {
    expect(signInBlocked(LOGIN, 'host-b')).toBeNull();
    expect(signInBlocked({...LOGIN, installed: false}, 'host-b')).toContain('not installed');
    expect(signInBlocked({...LOGIN, can_start_login: false}, 'host-b')).toBe(
      'Claude Code has no login command to start.',
    );
  });
});
