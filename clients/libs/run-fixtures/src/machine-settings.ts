/**
 * What the machines of a fixture world answer the machine Settings with: for each machine its
 * clusters and clouds (`compute.list`) and its agents' logins (`agent_logins.list`), where a
 * machine can be one whose helper predates either, plus how a connection test and a login start
 * answer for the places a journey names. Built from the shapes `compute.ts` and `agent-logins.ts`
 * hold valid against the helper contract, so a journey over several machines (one fully set up,
 * one empty, one too old) needs no helper. `machine-settings.test.ts` holds the world consistent.
 */
import type {
  AgentLogin,
  ClusterTestFailed,
  ClusterTestPassed,
  ComputeSettings,
  LoginStarted,
  LoginUnavailable,
} from '@vibesys/contracts/host-helper';
import {agentLogins, loginStarts} from './agent-logins.js';
import {
  clusterTestPassed,
  clusterTestPassedUntyped,
  clusterTestsFailed,
  computeSettings,
  computeSettingsEmpty,
} from './compute.js';

/** One machine's answers; null for what its helper predates. */
export interface MachineSettingsFixture {
  readonly compute: ComputeSettings | null;
  readonly logins: readonly AgentLogin[] | null;
}

/** Only Claude Code is installed, and it is not signed in. */
const CLAUDE_ONLY: readonly AgentLogin[] = agentLogins.map(login => {
  const {how: _how, ...rest} = login;
  return login.provider === 'claude'
    ? {...rest, state: 'signed_out', detail: 'Not logged in'}
    : {...rest, installed: false, state: 'unknown', detail: '', can_start_login: false};
});

/**
 * By host key: This Mac has clusters in both files, a Modal token and every kind of login;
 * `host-b` has nothing set up and one harness installed; `host-c` runs a VibeSys that predates
 * both features.
 */
export const machineSettingsWorld: Readonly<Record<string, MachineSettingsFixture>> = {
  local: {compute: computeSettings, logins: agentLogins},
  'ssh:host-b': {compute: computeSettingsEmpty, logins: CLAUDE_ONLY},
  'ssh:host-c': {compute: null, logins: null},
};

/**
 * A connection test's answer by the ssh host alias of the cluster tested (`''` for a login node):
 * a cluster whose hardware `sinfo` names, one whose GPUs it does not, and one for each stage a
 * test can fail at. An alias not named here cannot be reached.
 */
export const clusterTestsByAlias: Readonly<Record<string, ClusterTestPassed | ClusterTestFailed>> =
  {
    'lab-login': clusterTestPassed,
    '': clusterTestPassedUntyped,
    'no-scheduler': clusterTestsFailed[1] as ClusterTestFailed,
    'no-workspace': clusterTestsFailed[2] as ClusterTestFailed,
  };

/** What starting each harness's login answers; a provider not named has no login command. */
export const loginStartsByProvider: Readonly<Record<string, LoginStarted | LoginUnavailable>> = {
  claude: loginStarts[0] as LoginStarted,
  codex: loginStarts[1] as LoginStarted,
  copilot: loginStarts[1] as LoginStarted,
};
