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
    return this.invokeFromSender(
      7,
      'app://vibesys/welcome.html',
      rendererGeneration,
      channel,
      ...args,
    );
  }

  invokeFromUrl(
    senderUrl: string,
    rendererGeneration: string,
    channel: string,
    ...args: unknown[]
  ): Promise<WelcomeResult<unknown>> {
    return this.invokeFromSender(7, senderUrl, rendererGeneration, channel, ...args);
  }

  invokeFromSender(
    senderId: number,
    senderUrl: string,
    rendererGeneration: string,
    channel: string,
    ...args: unknown[]
  ): Promise<WelcomeResult<unknown>> {
    const handler = this.#handlers.get(channel);
    if (handler === undefined) throw new Error(`no handler for ${channel}`);
    return handler(
      {
        senderId,
        senderUrl,
        rendererGeneration,
      },
      ...args,
    );
  }
}

class FakeWelcomeDocument implements WelcomeDocument {
  #id: number | null = 7;
  readonly #newDocuments = new Set<(retiringGeneration: string | null) => void>();
  readonly #destroyed = new Set<() => void>();

  currentId(): number | null {
    return this.#id;
  }

  onNewDocument(listener: (retiringGeneration: string | null) => void): void {
    this.#newDocuments.add(listener);
  }

  onDestroyed(listener: () => void): void {
    this.#destroyed.add(listener);
  }

  navigate(retiringGeneration: string | null): void {
    for (const listener of this.#newDocuments) listener(retiringGeneration);
  }

  replace(id: number): void {
    this.#id = null;
    this.navigate(null);
    this.#id = id;
  }

  destroy(): void {
    for (const listener of this.#destroyed) listener();
  }
}

async function spin(): Promise<void> {
  for (let turn = 0; turn < 8; turn += 1) await Promise.resolve();
}

describe('bindWelcomeIpc', () => {
  test('admits only the welcome resource, independent of query or fragment', async () => {
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
      () => undefined,
      async () => undefined,
    );

    for (const url of [
      'app://vibesys/welcome.html',
      'app://vibesys/welcome.html?source=launch',
      'app://vibesys/welcome.html#recent',
    ]) {
      await expect(ipc.invokeFromUrl(url, '10:100', WELCOME_CHANNELS.hosts)).resolves.toMatchObject(
        {
          ok: true,
        },
      );
    }
    for (const url of [
      'app://vibesys/welcome.html-preview.html',
      'app://vibesys/welcome.html/child',
      'app://other/welcome.html',
      'app://user@vibesys/welcome.html',
      'app://vibesys:443/welcome.html',
      'https://vibesys/welcome.html',
      'not a url',
    ]) {
      await expect(ipc.invokeFromUrl(url, '10:100', WELCOME_CHANNELS.hosts)).resolves.toMatchObject(
        {
          ok: false,
          error: 'not the welcome view',
        },
      );
    }
    expect(answered).toEqual(['10:100', '10:100', '10:100']);
  });

  test('a replacement document waits for the retired generation before reusing a token', async () => {
    const ipc = new FakeWelcomeIpc();
    const document = new FakeWelcomeDocument();
    const firstReply = new Deferred();
    const retirement = new Deferred();
    const answered: Array<{readonly generation: string; readonly change: number}> = [];
    const revoked: string[] = [];
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
      generation => {
        revoked.push(generation);
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
    expect(revoked).toEqual(['10:100']);
    expect(retired).toEqual([]);
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
      () => undefined,
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
      () => undefined,
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

  test('a recreated welcome page revokes the old sender and admits only its successor', async () => {
    const ipc = new FakeWelcomeIpc();
    const document = new FakeWelcomeDocument();
    const retired: string[] = [];
    const answered: string[] = [];
    bindWelcomeIpc(
      ipc,
      document,
      'app://vibesys/welcome.html',
      async (_request, generation) => {
        answered.push(generation);
        return null;
      },
      () => undefined,
      async generation => {
        retired.push(generation);
      },
    );

    await ipc.invokeFromSender(7, 'app://vibesys/welcome.html', '10:100', WELCOME_CHANNELS.hosts);
    document.replace(8);

    await expect(
      ipc.invokeFromSender(7, 'app://vibesys/welcome.html', '10:100', WELCOME_CHANNELS.hosts),
    ).resolves.toMatchObject({ok: false, error: 'not the welcome view'});
    await expect(
      ipc.invokeFromSender(8, 'app://vibesys/welcome.html', '11:101', WELCOME_CHANNELS.hosts),
    ).resolves.toEqual({ok: true, value: null});
    expect(retired).toEqual(['10:100']);
    expect(answered).toEqual(['10:100', '11:101']);
  });
});
