import {describe, expect, it} from 'bun:test';
import {parseHelperMessage, parseHelperRequest} from '@vibesys/contracts/host-helper';
import {agentLogins, loginStarts} from './agent-logins.js';

const reply = (type: string, body: Record<string, unknown>): string =>
  JSON.stringify({type, request_id: 'r1', ...body});
const request = (type: string, body: Record<string, unknown>): string =>
  JSON.stringify({type, request_id: 'r1', deadline_ms: 5000, ...body});

describe('the agent login fixtures', () => {
  it('are what a helper that offers agent_logins may answer', () => {
    expect(() =>
      parseHelperMessage(reply('agent_logins.list', {logins: agentLogins})),
    ).not.toThrow();
    for (const result of loginStarts) {
      expect(() => parseHelperMessage(reply('agent_logins.start', {result}))).not.toThrow();
    }
  });

  it('are what a client asks with', () => {
    expect(() => parseHelperRequest(request('agent_logins.list', {}))).not.toThrow();
    const start = request('agent_logins.start', {provider: 'claude'});
    expect(() => parseHelperRequest(start)).not.toThrow();
  });

  it('cover every state, both ways to hold a key, and a missing CLI', () => {
    expect(new Set(agentLogins.map(login => login.state))).toEqual(
      new Set(['signed_in', 'signed_out', 'unknown']),
    );
    const hows = agentLogins.flatMap(login => (login.how ? [login.how] : []));
    expect(new Set(hows.map(how => how.kind))).toEqual(new Set(['account', 'api_key']));
    expect(hows.some(how => how.kind === 'api_key' && how.variable)).toBe(true);
    expect(hows.some(how => how.kind === 'api_key' && how.file)).toBe(true);
    expect(agentLogins.some(login => !login.installed)).toBe(true);
  });

  it('only say how a signed-in harness is signed in, and never carry a secret', () => {
    for (const login of agentLogins) {
      if (login.how) expect(login.state).toBe('signed_in');
      if (!login.installed) expect(login.state).toBe('unknown');
    }
    expect(JSON.stringify(agentLogins)).not.toMatch(/sk-|secret/i);
  });

  it('are strict: an unknown key is a parse error', () => {
    const logins = agentLogins.map(login => ({...login, token: 'x'}));
    expect(() => parseHelperMessage(reply('agent_logins.list', {logins}))).toThrow();
  });
});
