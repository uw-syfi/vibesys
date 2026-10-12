import {describe, it} from 'node:test';
import {DEFAULT_REQUEST_POLICY, REQUEST_POLICIES, resolveRequestPolicy} from './request-policy.js';
import {expect} from './test-support/expect.js';

describe('resolveRequestPolicy', () => {
  it('marks reads idempotent by default', () => {
    expect(resolveRequestPolicy('query.snapshot')).toEqual({
      idempotent: true,
      dedicatedConnection: false,
    });
  });

  it('follows the idempotency table for named commands', () => {
    expect(resolveRequestPolicy('command.pause').idempotent).toBe(true);
    expect(resolveRequestPolicy('command.resume').idempotent).toBe(true);
    expect(resolveRequestPolicy('command.steer').idempotent).toBe(false);
    expect(resolveRequestPolicy('query.chat').idempotent).toBe(false);
    expect(resolveRequestPolicy('query.chat_thread_create').idempotent).toBe(false);
  });

  it('runs chat on a dedicated connection', () => {
    expect(resolveRequestPolicy('query.chat').dedicatedConnection).toBe(true);
  });

  it('lets a call override the connection and deadline but not idempotency', () => {
    const policy = resolveRequestPolicy('command.steer', {dedicatedConnection: true, timeoutMs: 5});
    expect(policy).toEqual({idempotent: false, dedicatedConnection: true, timeoutMs: 5});
  });

  it('treats a type absent from the table as the read default', () => {
    expect(resolveRequestPolicy('query.design')).toEqual(DEFAULT_REQUEST_POLICY);
  });

  it('exposes the table as data a double-submit guard can read', () => {
    expect(REQUEST_POLICIES['command.pause']?.idempotent).toBe(true);
    expect(REQUEST_POLICIES['command.steer']?.idempotent).toBe(false);
  });
});
