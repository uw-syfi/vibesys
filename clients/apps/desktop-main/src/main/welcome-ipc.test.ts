import {describe, expect, test} from 'bun:test';
import {WELCOME_CHANNELS, type WelcomeResult} from '@vibesys/ipc';
import {
  bindWelcomeIpc,
  type WelcomeDocument,
  type WelcomeInvokeEvent,
  type WelcomeIpc,
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

describe('bindWelcomeIpc', () => {
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
