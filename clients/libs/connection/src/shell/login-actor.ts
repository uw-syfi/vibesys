/** A main-owned login. Subscriptions do not own the host process. */
import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import type {LoginStarted, LoginUnavailable} from '@vibesys/contracts/host-helper';
import type {HostId} from '@vibesys/hosts';
import {hostKey} from '@vibesys/hosts';
import {
  decodeHostWatch,
  encodeHostWatch,
  LoginUnavailableReason as LoginRefusal,
  LoginRequestSchema,
  LoginResponseSchema,
} from '@vibesys/proto';
import {createActor, fromPromise} from 'xstate';
import {loginMachine} from '../state/login.js';
import {requireCheckout} from './registry-reads.js';

export type LoginStart = LoginStarted | LoginUnavailable;

export interface LoginPorts {
  checkout(host: HostId): Promise<string | null>;
  stream(
    host: HostId,
    bytes: Uint8Array,
    signal: AbortSignal,
    receive: (bytes: Uint8Array) => void,
  ): Promise<void>;
}

interface Running {
  readonly actor: ReturnType<typeof createActor<typeof loginMachine>>;
  readonly first: Promise<LoginStart>;
}

const REFUSALS: Record<number, LoginUnavailable['reason'] | undefined> = {
  [LoginRefusal.NOT_INSTALLED]: 'not_installed',
  [LoginRefusal.NO_LOGIN_COMMAND]: 'no_login_command',
  [LoginRefusal.NEEDS_TERMINAL]: 'needs_terminal',
  [LoginRefusal.START_FAILED]: 'start_failed',
};

function loginKey(host: HostId, provider: string): string {
  return `${hostKey(host)}\0${provider}`;
}

/** Holds each host login until completion, cancellation, or loss. Network loss does not start another. */
export class LoginActor {
  readonly #running = new Map<string, Running>();
  readonly #subscribers = new Map<string, number>();
  #next = 1;
  #closed = false;

  constructor(private readonly ports: LoginPorts) {}

  /** Start `provider`'s login, or join the one already running. The promise resolves on the first event. */
  start(host: HostId, provider: string): Promise<LoginStart> {
    if (this.#closed) return Promise.reject(new Error('login actor closed'));
    const key = loginKey(host, provider);
    const existing = this.#running.get(key);
    if (existing !== undefined) return existing.first;
    let resolveFirst: (value: LoginStart) => void = () => {};
    let rejectFirst: (error: unknown) => void = () => {};
    const first = new Promise<LoginStart>((resolve, reject) => {
      resolveFirst = resolve;
      rejectFirst = reject;
    });
    let settled = false;
    const settle = (value?: LoginStart, error?: unknown): void => {
      if (settled) return;
      settled = true;
      if (error !== undefined) rejectFirst(error);
      else if (value !== undefined) resolveFirst(value);
    };
    const actor = createActor(
      loginMachine.provide({
        actors: {
          watch: fromPromise(async ({signal}) => {
            try {
              const checkout = requireCheckout(await this.ports.checkout(host), null);
              if (signal.aborted) return 'completed' as const;
              const requestId = `login-${this.#next}`;
              this.#next += 1;
              const bytes = encodeHostWatch(
                checkout,
                'Login',
                toBinary(LoginRequestSchema, create(LoginRequestSchema, {requestId, provider})),
              );
              await this.ports.stream(host, bytes, signal, payload => {
                const event = fromBinary(LoginResponseSchema, decodeHostWatch('Login', payload));
                if (event.requestId !== requestId) throw new Error('login event identity mismatch');
                const body = event.body;
                if (body.case === 'started') {
                  const instruction = body.value.instruction;
                  settle({
                    outcome: 'started',
                    ...(instruction === undefined || instruction === '' ? {} : {instruction}),
                  });
                  return;
                }
                if (body.case === 'unavailable') {
                  settle(refused(body.value));
                  actor.send({type: 'unavailable'});
                  return;
                }
                throw new Error('login event has no body');
              });
              return 'completed' as const;
            } catch (error) {
              if (!settled) settle(undefined, error);
              if (signal.aborted) return 'completed' as const;
              return 'lost' as const;
            }
          }),
        },
      }),
    );
    actor.subscribe(snapshot => {
      if (snapshot.status !== 'done') return;
      this.#running.delete(key);
      if (!settled) settle(undefined, new Error(`login ${snapshot.value}`));
    });
    this.#running.set(key, {actor, first});
    actor.start();
    return first;
  }

  /** Count a subscriber. The returned function drops that count and does not cancel the login. */
  subscribe(host: HostId, provider: string): () => void {
    const key = loginKey(host, provider);
    this.#subscribers.set(key, (this.#subscribers.get(key) ?? 0) + 1);
    let dropped = false;
    return () => {
      if (dropped) return;
      dropped = true;
      const count = (this.#subscribers.get(key) ?? 1) - 1;
      if (count <= 0) this.#subscribers.delete(key);
      else this.#subscribers.set(key, count);
    };
  }

  /** How many panels are watching this login. Zero does not mean it stopped. */
  subscribers(host: HostId, provider: string): number {
    return this.#subscribers.get(loginKey(host, provider)) ?? 0;
  }

  /** Stop one login and reap it. */
  cancel(host: HostId, provider: string): void {
    this.#running.get(loginKey(host, provider))?.actor.send({type: 'cancel'});
  }

  /** Stop every login on `host`. Host removal and checkout replacement use this. */
  cancelHost(host: HostId): void {
    const prefix = `${hostKey(host)}\0`;
    for (const [key, running] of this.#running) {
      if (key.startsWith(prefix)) running.actor.send({type: 'cancel'});
    }
  }

  /** Stop every login. Shutdown uses this. */
  close(): void {
    this.#closed = true;
    for (const running of this.#running.values()) running.actor.send({type: 'cancel'});
  }
}

function refused(body: {
  readonly reason: LoginRefusal;
  readonly command?: string | undefined;
  readonly detail: string;
}): LoginUnavailable {
  const reason = REFUSALS[body.reason];
  if (reason === undefined) throw new Error('unknown login refusal');
  return {
    outcome: 'unavailable',
    reason,
    ...(body.command === undefined || body.command === '' ? {} : {command: body.command}),
    ...(body.detail === '' ? {} : {detail: body.detail}),
  };
}
