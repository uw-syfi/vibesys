import {describe, expect, test} from 'bun:test';
import type {AgentLogin} from '@vibesys/ipc';
import fc from 'fast-check';
import {
  failureWords,
  hardwareModels,
  hardwareWords,
  loginSteps,
  loginWords,
  machinesDetail,
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
  test('a cluster says how it is reached in a few words, never a sentence', () => {
    const base = {name: 'c', workspace: '/w', sbatch_options: []};
    expect(reachWords({...base, reach: {kind: 'ssh', host: 'lab-login'}})).toBe('SSH · lab-login');
    expect(reachWords({...base, reach: {kind: 'login_node'}})).toBe('Login node');
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
      how: {text: 'Account', identifier: null},
    });
    expect(
      loginWords({...LOGIN, how: {kind: 'api_key', variable: 'ANTHROPIC_API_KEY'}}).how,
    ).toEqual({text: 'API key', identifier: 'ANTHROPIC_API_KEY'});
    expect(loginWords({...LOGIN, how: {kind: 'api_key', file: '.gemini/creds.json'}}).how).toEqual({
      text: 'Credentials',
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

describe('the Machines entry', () => {
  test('counts the saved machines and agrees in number with those needing attention', () => {
    expect(machinesDetail(2, 0)).toBe('2 saved');
    expect(machinesDetail(2, 1)).toBe('2 saved · 1 needs attention');
    expect(machinesDetail(5, 2)).toBe('5 saved · 2 need attention');
    for (let attention = 1; attention <= 6; attention += 1) {
      const words = machinesDetail(6, attention);
      expect(words).toContain(attention === 1 ? ' 1 needs ' : ` ${attention} need `);
    }
  });
});

describe('a failed reading', () => {
  test('a connection failure is its status word with the raw text for the tooltip', () => {
    const raw = 'the host helper for /srv/x exited with 1: ModuleNotFoundError';
    const words = failureWords({error: raw, cause: 'incompatible'});
    expect(words).toEqual({body: 'Incompatible', hint: raw});
    expect(JSON.stringify(words.body)).not.toContain('/srv');
  });

  test('any other failure is shown in the words it came with', () => {
    expect(failureWords({error: 'No such cluster.'})).toEqual({body: 'No such cluster.'});
  });
});

describe('loginSteps', () => {
  test('property: the host’s line gives back its link and its code', () => {
    fc.assert(
      fc.property(
        fc.webUrl(),
        fc.option(fc.stringMatching(/^[A-Z0-9][A-Z0-9-]{1,10}$/), {nil: null}),
        (link, code) => {
          // The line `login_instruction` words on the host.
          const line = code === null ? `Open ${link}` : `Open ${link} and enter the code ${code}`;
          expect(loginSteps(line)).toEqual({link, code});
        },
      ),
      {numRuns: 100, seed: 3515},
    );
  });

  test('a line of another shape is not taken apart', () => {
    for (const line of [
      '',
      'Run claude login',
      'Open',
      'open https://a.example',
      'Visit https://a.example',
    ]) {
      expect(loginSteps(line), line).toBeNull();
    }
  });
});
