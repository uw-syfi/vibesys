/**
 * What a helper answers `agent_logins.list` and `agent_logins.start` with (feature `agent_logins`):
 * fixed copies of every shape the contract names (signed in with an account, with an API key held
 * in a variable or a file, signed out, a login the CLI cannot report, a CLI that is not installed),
 * so a screen that shows each harness's login on a machine can be built and checked without a
 * helper. A key is only ever named by where it is held, never shown. `agent-logins.test.ts` holds
 * each valid against the helper contract.
 */
import type {AgentLogin, LoginStarted, LoginUnavailable} from '@vibesys/contracts/host-helper';

export const agentLogins: readonly AgentLogin[] = [
  {
    provider: 'claude',
    provider_name: 'Claude Code',
    installed: true,
    state: 'signed_in',
    how: {kind: 'account'},
    api_key_variables: ['ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_API_KEY', 'CLAUDE_CODE_OAUTH_TOKEN'],
    detail: 'Logged in with claude.ai',
    can_start_login: true,
  },
  {
    provider: 'codex',
    provider_name: 'Codex',
    installed: true,
    state: 'signed_in',
    how: {kind: 'api_key', variable: 'OPENAI_API_KEY'},
    api_key_variables: ['CODEX_API_KEY', 'OPENAI_API_KEY'],
    detail: 'Logged in using an API key',
    can_start_login: true,
  },
  {
    provider: 'gemini',
    provider_name: 'Gemini CLI',
    installed: true,
    state: 'signed_in',
    how: {kind: 'api_key', file: '.gemini/oauth_creds.json'},
    api_key_variables: ['GEMINI_API_KEY', 'GOOGLE_API_KEY'],
    detail: '',
    can_start_login: false,
  },
  {
    provider: 'copilot',
    provider_name: 'GitHub Copilot',
    installed: true,
    state: 'signed_out',
    api_key_variables: ['COPILOT_GITHUB_TOKEN', 'GH_TOKEN', 'GITHUB_TOKEN'],
    detail: 'Not logged in',
    can_start_login: true,
  },
  {
    provider: 'opencode',
    provider_name: 'OpenCode',
    installed: true,
    state: 'unknown',
    api_key_variables: [],
    detail: 'this CLI has no login status command',
    can_start_login: false,
  },
  {
    provider: 'aider',
    provider_name: 'Aider',
    installed: false,
    state: 'unknown',
    api_key_variables: [],
    detail: '',
    can_start_login: false,
  },
];

/** The answers to `agent_logins.start`: started with what to do next, and each reason it cannot. */
export const loginStarts: readonly (LoginStarted | LoginUnavailable)[] = [
  {outcome: 'started', instruction: 'Open https://example.com/device and enter the code AB12'},
  {outcome: 'started'},
  {outcome: 'unavailable', reason: 'not_installed'},
  {outcome: 'unavailable', reason: 'no_login_command'},
];
