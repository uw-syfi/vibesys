import {describe, expect, test} from 'bun:test';
import {Seeded} from '@vibesys/core-state/testing';
import {isInstanceId, isRunId, isTaskName} from '@vibesys/entities';
import {WELCOME_CHANNELS, type WelcomeResult} from '@vibesys/ipc';
import {
  bindWelcomeIpc,
  parseWelcomeRequest,
  type WelcomeDocument,
  type WelcomeInvokeEvent,
  type WelcomeIpc,
  type WelcomeRequest,
} from './welcome-ipc.js';

class Deferred {
  readonly promise: Promise<void>;
  #resolve: () => void = () => undefined;

  constructor() {
    this.promise = new Promise(resolve => {
      this.#resolve = resolve;
    });
  }

  resolve(): void {
    this.#resolve();
  }
}

class FakeWelcomeIpc implements WelcomeIpc {
  readonly #handlers = new Map<
    string,
    (event: WelcomeInvokeEvent, ...args: unknown[]) => Promise<WelcomeResult<unknown>>
  >();

  handle(
    channel: string,
    listener: (event: WelcomeInvokeEvent, ...args: unknown[]) => Promise<WelcomeResult<unknown>>,
  ): void {
    this.#handlers.set(channel, listener);
  }

  invokeFrom(
    rendererGeneration: string,
    channel: string,
    ...args: unknown[]
  ): Promise<WelcomeResult<unknown>> {
    const handler = this.#handlers.get(channel);
    if (handler === undefined) throw new Error(`no handler for ${channel}`);
    return handler(
      {
        senderId: 7,
        senderUrl: 'app://vibesys/welcome.html',
        rendererGeneration,
      },
      ...args,
    );
  }
}

class FakeWelcomeDocument implements WelcomeDocument {
  readonly id = 7;
  readonly #newDocuments = new Set<(retiringGeneration: string | null) => void>();
  readonly #destroyed = new Set<() => void>();

  onNewDocument(listener: (retiringGeneration: string | null) => void): void {
    this.#newDocuments.add(listener);
  }

  onDestroyed(listener: () => void): void {
    this.#destroyed.add(listener);
  }

  navigate(retiringGeneration: string | null): void {
    for (const listener of this.#newDocuments) listener(retiringGeneration);
  }

  destroy(): void {
    for (const listener of this.#destroyed) listener();
  }
}

async function spin(): Promise<void> {
  for (let turn = 0; turn < 8; turn += 1) await Promise.resolve();
}

const VALUES: readonly unknown[] = [
  undefined,
  null,
  0,
  7,
  -1,
  1.5,
  true,
  '',
  'local',
  'ssh:gpu-box',
  'ssh:-oProxyCommand=x',
  'remote',
  '0123456789ab',
  'ZZZZ',
  'spsc',
  'Bad Task',
  '/srv/proj',
  'relative/proj',
  '-rf',
  'run-1',
  '--rounds 2',
  {},
  [],
];

/** What every accepted request guarantees, whatever the view sent. */
function checkAccepted(request: WelcomeRequest): void {
  if ('instance' in request) expect(isInstanceId(request.instance)).toBe(true);
  if (request.kind === 'start') {
    expect(isTaskName(request.task)).toBe(true);
    expect(Number.isSafeInteger(request.start) && request.start > 0).toBe(true);
  }
  if (request.kind === 'set-checkout' || request.kind === 'cancel-checkout') {
    expect(Number.isSafeInteger(request.change) && request.change > 0).toBe(true);
  }
  if (request.kind === 'resume') expect(isRunId(request.run)).toBe(true);
  if ('project' in request) expect(request.project.startsWith('/')).toBe(true);
  if ('host' in request) expect(['local', 'ssh']).toContain(request.host.kind);
}

describe('parseWelcomeRequest', () => {
  test('property: any arguments yield a validated request or an error naming the problem', () => {
    const random = new Seeded(21);
    const channels = Object.values(WELCOME_CHANNELS);
    for (let round = 0; round < 5_000; round += 1) {
      const channel = random.pick(channels);
      const args = Array.from({length: random.int(6)}, () => random.pick(VALUES));
      let request: WelcomeRequest;
      try {
        request = parseWelcomeRequest(channel, args);
      } catch (error) {
        expect(error).toBeInstanceOf(Error);
        expect((error as Error).message).not.toBe('');
        continue;
      }
      checkAccepted(request);
    }
  });

  test('a start carries the task, the split run arguments, and its token', () => {
    expect(
      parseWelcomeRequest(WELCOME_CHANNELS.start, ['local', '/p', 'spsc', '--rounds 2', 3]),
    ).toEqual({
      kind: 'start',
      host: {kind: 'local'},
      project: '/p',
      task: 'spsc',
      args: ['--rounds', '2'],
      start: 3,
    });
    expect(() =>
      parseWelcomeRequest(WELCOME_CHANNELS.resume, ['local', '/p', '-oProxyCommand']),
    ).toThrow('a run id is 1-128 lowercase');
  });

  test('checkout changes and cancellations carry the same validated operation token', () => {
    expect(parseWelcomeRequest(WELCOME_CHANNELS.setCheckout, ['local', '/new', 7])).toEqual({
      kind: 'set-checkout',
      host: {kind: 'local'},
      path: '/new',
      change: 7,
    });
    expect(parseWelcomeRequest(WELCOME_CHANNELS.cancelCheckout, [7])).toEqual({
      kind: 'cancel-checkout',
      change: 7,
    });
    expect(() => parseWelcomeRequest(WELCOME_CHANNELS.cancelCheckout, [0])).toThrow(
      'a checkout change is named by a positive integer',
    );
  });

  test('a replacement document waits for the retired generation before reusing a token', async () => {
    const ipc = new FakeWelcomeIpc();
    const document = new FakeWelcomeDocument();
    const firstReply = new Deferred();
    const retirement = new Deferred();
    const answered: Array<{readonly generation: string; readonly change: number}> = [];
    const retired: string[] = [];
    bindWelcomeIpc(
      ipc,
      document,
      'app://vibesys/welcome.html',
      async (request, generation) => {
        if (request.kind !== 'set-checkout') return null;
        answered.push({generation, change: request.change});
        if (answered.length === 1) await firstReply.promise;
        return '/checkout';
      },
      async generation => {
        retired.push(generation);
        await retirement.promise;
      },
    );

    const first = ipc.invokeFrom('10:100', WELCOME_CHANNELS.setCheckout, 'local', '/old', 1);
    await spin();
    expect(answered).toEqual([{generation: '10:100', change: 1}]);

    document.navigate('10:100');
    const replacement = ipc.invokeFrom('11:101', WELCOME_CHANNELS.setCheckout, 'local', '/new', 1);
    await spin();
    expect(retired).toEqual(['10:100']);
    expect(answered).toEqual([{generation: '10:100', change: 1}]);

    retirement.resolve();
    await spin();
    expect(answered).toEqual([
      {generation: '10:100', change: 1},
      {generation: '11:101', change: 1},
    ]);
    firstReply.resolve();
    await Promise.all([first, replacement]);
  });

  test('a navigation with no retiring frame rejects a request waiting for admission', async () => {
    const ipc = new FakeWelcomeIpc();
    const document = new FakeWelcomeDocument();
    const answered: string[] = [];
    bindWelcomeIpc(
      ipc,
      document,
      'app://vibesys/welcome.html',
      async (_request, generation) => {
        answered.push(generation);
        return null;
      },
      async () => undefined,
    );

    const obsolete = ipc.invokeFrom('10:100', WELCOME_CHANNELS.hosts);
    document.navigate(null);

    await expect(obsolete).resolves.toEqual({
      ok: false,
      error: 'welcome view was replaced',
      authNeeded: false,
    });
    expect(answered).toEqual([]);
    await expect(ipc.invokeFrom('11:101', WELCOME_CHANNELS.hosts)).resolves.toEqual({
      ok: true,
      value: null,
    });
    expect(answered).toEqual(['11:101']);
  });

  test('destroying the renderer retires its operation generation', async () => {
    const ipc = new FakeWelcomeIpc();
    const document = new FakeWelcomeDocument();
    const retired: string[] = [];
    bindWelcomeIpc(
      ipc,
      document,
      'app://vibesys/welcome.html',
      async () => null,
      async generation => {
        retired.push(generation);
      },
    );

    await ipc.invokeFrom('10:100', WELCOME_CHANNELS.hosts);
    document.destroy();
    await spin();
    expect(retired).toEqual(['10:100']);
    await expect(ipc.invokeFrom('10:100', WELCOME_CHANNELS.hosts)).resolves.toMatchObject({
      ok: false,
      error: 'welcome view was replaced',
    });
  });
});
