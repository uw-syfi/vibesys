import {describe, it} from 'node:test';
import {
  type AbortSignalLike,
  DEFAULT_REQUEST_POLICY,
  REQUEST_POLICIES,
  registerAbort,
  resolveRequestPolicy,
} from './index.js';
import {expect} from './test-support/expect.js';

class ScriptedAbortSignal implements AbortSignalLike {
  aborted = false;
  readonly #listeners = new Set<() => void>();

  constructor(private readonly registration: 'none' | 'dispatch' | 'silent') {}

  get listenerCount(): number {
    return this.#listeners.size;
  }

  addEventListener(_type: 'abort', listener: () => void): void {
    this.#listeners.add(listener);
    if (this.registration === 'none') return;
    this.aborted = true;
    if (this.registration === 'dispatch') listener();
  }

  removeEventListener(_type: 'abort', listener: () => void): void {
    this.#listeners.delete(listener);
  }

  abort(): void {
    this.aborted = true;
    for (const listener of [...this.#listeners]) listener();
  }
}

class ThrowingAbortSignal implements AbortSignalLike {
  aborted = false;
  readonly registrationError = new Error('listener registration failed');
  readonly removalError = new Error('listener removal failed');
  readonly #listeners = new Set<() => void>();
  removeCalls = 0;

  constructor(private readonly throwOnAdd: boolean) {}

  addEventListener(_type: 'abort', listener: () => void): void {
    this.#listeners.add(listener);
    if (this.throwOnAdd) throw this.registrationError;
  }

  removeEventListener(_type: 'abort', _listener: () => void): void {
    this.removeCalls += 1;
    throw this.removalError;
  }

  abort(): void {
    this.aborted = true;
    for (const listener of [...this.#listeners]) listener();
  }
}

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

  it('gives a read the read policy', () => {
    expect(resolveRequestPolicy('query.design')).toEqual(DEFAULT_REQUEST_POLICY);
  });

  it('exposes the table as data a double-submit guard can read', () => {
    expect(REQUEST_POLICIES['command.pause']?.idempotent).toBe(true);
    expect(REQUEST_POLICIES['command.steer']?.idempotent).toBe(false);
    expect(REQUEST_POLICIES['command.control']?.idempotent).toBe(false);
  });
});

describe('registerAbort', () => {
  for (const behavior of ['dispatch', 'silent'] as const) {
    it(`keeps an abort during ${behavior} registration with the unpublished owner`, () => {
      const signal = new ScriptedAbortSignal(behavior);
      let notifications = 0;

      const registration = registerAbort(signal, () => {
        notifications += 1;
      });

      expect(registration.aborted).toBe(true);
      expect(notifications).toBe(0);
      expect(signal.listenerCount).toBe(0);
      registration.detach();
      signal.abort();
      expect(notifications).toBe(0);
    });
  }

  it('delivers one later abort and detaches it', () => {
    const signal = new ScriptedAbortSignal('none');
    let notifications = 0;
    const registration = registerAbort(signal, () => {
      notifications += 1;
    });

    expect(registration.aborted).toBe(false);
    expect(signal.listenerCount).toBe(1);
    signal.abort();
    signal.abort();
    registration.detach();

    expect(notifications).toBe(1);
    expect(signal.listenerCount).toBe(0);
  });

  it('detaches idempotently and fences a listener whose physical removal throws', () => {
    const signal = new ThrowingAbortSignal(false);
    let notifications = 0;
    const registration = registerAbort(signal, () => {
      notifications += 1;
    });

    expect(() => registration.detach()).not.toThrow();
    expect(() => registration.detach()).not.toThrow();
    expect(signal.removeCalls).toBe(1);
    expect(() => signal.abort()).not.toThrow();
    expect(notifications).toBe(0);
  });

  it('preserves an add failure when rollback removal also throws', () => {
    const signal = new ThrowingAbortSignal(true);

    expect(() => registerAbort(signal, () => undefined)).toThrow(signal.registrationError);
    expect(signal.removeCalls).toBe(1);
    expect(() => signal.abort()).not.toThrow();
  });
});
