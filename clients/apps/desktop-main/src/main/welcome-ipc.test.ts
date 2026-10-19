import {describe, expect, test} from 'bun:test';
import {ConnectionError} from '@vibesys/connection';
import {WELCOME_CHANNELS, type WelcomeResult} from '@vibesys/ipc';
import {
  type WelcomeDocument,
  type WelcomeInvokeEvent,
  type WelcomeIpc,
  WelcomeRouter,
} from './welcome-ipc.js';

class Deferred {
  readonly promise: Promise<void>;
  #resolve: () => void = () => undefined;
  #reject: (error: unknown) => void = () => undefined;

  constructor() {
    this.promise = new Promise((resolve, reject) => {
      this.#resolve = resolve;
      this.#reject = reject;
    });
  }

  resolve(): void {
    this.#resolve();
  }

  reject(error: unknown): void {
    this.#reject(error);
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
      'app://vibesys/shell.html',
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

describe('WelcomeRouter', () => {
  test('admits only the welcome resource, independent of query or fragment', async () => {
    const ipc = new FakeWelcomeIpc();
    const document = new FakeWelcomeDocument();
    const answered: string[] = [];
    new WelcomeRouter(ipc).bind({
      document: document,
      url: 'app://vibesys/shell.html',
      answer: async (_request, generation) => {
        answered.push(generation);
        return null;
      },
      retire: async () => undefined,
    });

    for (const url of [
      'app://vibesys/shell.html',
      'app://vibesys/shell.html?source=launch',
      'app://vibesys/shell.html#recent',
    ]) {
      await expect(
        ipc.invokeFromUrl(url, '10:100', WELCOME_CHANNELS.releaseLogs),
      ).resolves.toMatchObject({
        ok: true,
      });
    }
    for (const url of [
      'app://vibesys/shell.html-preview.html',
      'app://vibesys/shell.html/child',
      'app://other/shell.html',
      'app://user@vibesys/shell.html',
      'app://vibesys:443/shell.html',
      'https://vibesys/shell.html',
      'not a url',
    ]) {
      await expect(
        ipc.invokeFromUrl(url, '10:100', WELCOME_CHANNELS.releaseLogs),
      ).resolves.toMatchObject({
        ok: false,
        error: 'not the welcome view',
      });
    }
    expect(answered).toEqual(['10:100', '10:100', '10:100']);
  });

  test('a replacement document waits for the retired generation before reusing a token', async () => {
    const ipc = new FakeWelcomeIpc();
    const document = new FakeWelcomeDocument();
    const firstReply = new Deferred();
    const retirement = new Deferred();
    const answered: Array<{readonly generation: string; readonly change: number}> = [];
    const retired: string[] = [];
    new WelcomeRouter(ipc).bind({
      document: document,
      url: 'app://vibesys/shell.html',
      answer: async (request, generation) => {
        if (request.kind !== 'set-checkout') return null;
        answered.push({generation, change: request.change});
        if (answered.length === 1) await firstReply.promise;
        return '/checkout';
      },
      retire: async generation => {
        retired.push(generation);
        await retirement.promise;
      },
    });

    const first = ipc.invokeFrom('10:100', WELCOME_CHANNELS.setCheckout, 'local', '/old', 1);
    await spin();
    expect(answered).toEqual([{generation: '10:100', change: 1}]);

    document.navigate('10:100');
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

  test('one rejected generation cannot skip a sibling retirement or poison its successor', async () => {
    const ipc = new FakeWelcomeIpc();
    const document = new FakeWelcomeDocument();
    const firstRetirement = new Deferred();
    const secondRetirement = new Deferred();
    const retirementsStarted = new Deferred();
    const retired: string[] = [];
    const answered: string[] = [];
    new WelcomeRouter(ipc).bind({
      document: document,
      url: 'app://vibesys/shell.html',
      answer: async (_request, generation) => {
        answered.push(generation);
        return null;
      },
      retire: async generation => {
        retired.push(generation);
        if (retired.length === 2) retirementsStarted.resolve();
        await (generation === '10:100' ? firstRetirement.promise : secondRetirement.promise);
      },
    });
    await ipc.invokeFrom('10:100', WELCOME_CHANNELS.releaseLogs);
    await ipc.invokeFrom('10:101', WELCOME_CHANNELS.releaseLogs);

    document.navigate(null);
    let successorResult: WelcomeResult<unknown> | undefined;
    const successor = ipc.invokeFrom('11:102', WELCOME_CHANNELS.releaseLogs);
    void successor.then(result => {
      successorResult = result;
    });
    await retirementsStarted.promise;
    firstRetirement.reject(new Error('first retirement failed'));
    await spin();

    expect(retired).toEqual(['10:100', '10:101']);
    expect(successorResult).toBeUndefined();
    expect(answered).toEqual(['10:100', '10:101']);
    secondRetirement.resolve();
    await expect(successor).resolves.toEqual({ok: true, value: null});
    expect(answered).toEqual(['10:100', '10:101', '11:102']);
  });

  test('a navigation with no retiring frame rejects a request waiting for admission', async () => {
    const ipc = new FakeWelcomeIpc();
    const document = new FakeWelcomeDocument();
    const answered: string[] = [];
    new WelcomeRouter(ipc).bind({
      document: document,
      url: 'app://vibesys/shell.html',
      answer: async (_request, generation) => {
        answered.push(generation);
        return null;
      },
      retire: async () => undefined,
    });

    const obsolete = ipc.invokeFrom('10:100', WELCOME_CHANNELS.releaseLogs);
    document.navigate(null);

    await expect(obsolete).resolves.toEqual({
      ok: false,
      error: 'welcome view was replaced',
      authNeeded: false,
    });
    expect(answered).toEqual([]);
    await expect(ipc.invokeFrom('11:101', WELCOME_CHANNELS.releaseLogs)).resolves.toEqual({
      ok: true,
      value: null,
    });
    expect(answered).toEqual(['11:101']);
  });

  test('a failure of the connection carries its typed cause, however it was reworded', async () => {
    const causes = ['link', 'auth', 'incompatible', 'run-gone', 'denied', 'capacity'] as const;
    for (const reason of causes) {
      const ipc = new FakeWelcomeIpc();
      new WelcomeRouter(ipc).bind({
        document: new FakeWelcomeDocument(),
        url: 'app://vibesys/shell.html',
        answer: async () => {
          throw new ConnectionError(reason, 'the helper at /srv/x exited with 1').reworded(
            'Could not describe: the helper at /srv/x exited with 1',
          );
        },
        retire: async () => undefined,
      });
      await expect(ipc.invokeFrom('10:100', WELCOME_CHANNELS.releaseLogs)).resolves.toEqual({
        ok: false,
        error: 'Could not describe: the helper at /srv/x exited with 1',
        authNeeded: reason === 'auth',
        cause: reason,
      });
    }
  });

  test("a failure that is not the connection's carries no cause", async () => {
    const ipc = new FakeWelcomeIpc();
    new WelcomeRouter(ipc).bind({
      document: new FakeWelcomeDocument(),
      url: 'app://vibesys/shell.html',
      answer: async () => {
        throw new Error('No such project.');
      },
      retire: async () => undefined,
    });
    await expect(ipc.invokeFrom('10:100', WELCOME_CHANNELS.releaseLogs)).resolves.toEqual({
      ok: false,
      error: 'No such project.',
      authNeeded: false,
    });
  });

  test('destroying the renderer retires its operation generation', async () => {
    const ipc = new FakeWelcomeIpc();
    const document = new FakeWelcomeDocument();
    const retired: string[] = [];
    new WelcomeRouter(ipc).bind({
      document: document,
      url: 'app://vibesys/shell.html',
      answer: async () => null,
      retire: async generation => {
        retired.push(generation);
      },
    });

    await ipc.invokeFrom('10:100', WELCOME_CHANNELS.releaseLogs);
    document.destroy();
    await spin();
    expect(retired).toEqual(['10:100']);
    // The closed window's binding is gone: nothing answers its sender any more.
    await expect(ipc.invokeFrom('10:100', WELCOME_CHANNELS.releaseLogs)).resolves.toMatchObject({
      ok: false,
      error: 'not the welcome view',
    });
  });

  test('a recreated welcome page revokes the old sender and admits only its successor', async () => {
    const ipc = new FakeWelcomeIpc();
    const document = new FakeWelcomeDocument();
    const retired: string[] = [];
    const answered: string[] = [];
    new WelcomeRouter(ipc).bind({
      document: document,
      url: 'app://vibesys/shell.html',
      answer: async (_request, generation) => {
        answered.push(generation);
        return null;
      },
      retire: async generation => {
        retired.push(generation);
      },
    });

    await ipc.invokeFromSender(
      7,
      'app://vibesys/shell.html',
      '10:100',
      WELCOME_CHANNELS.releaseLogs,
    );
    document.replace(8);

    await expect(
      ipc.invokeFromSender(7, 'app://vibesys/shell.html', '10:100', WELCOME_CHANNELS.releaseLogs),
    ).resolves.toMatchObject({ok: false, error: 'not the welcome view'});
    await expect(
      ipc.invokeFromSender(8, 'app://vibesys/shell.html', '11:101', WELCOME_CHANNELS.releaseLogs),
    ).resolves.toEqual({ok: true, value: null});
    expect(retired).toEqual(['10:100']);
    expect(answered).toEqual(['10:100', '11:101']);
  });

  test('routes each request to the window that sent it, and a closed window to nobody', async () => {
    const ipc = new FakeWelcomeIpc();
    const router = new WelcomeRouter(ipc);
    const main = new FakeWelcomeDocument();
    const run = new FakeWelcomeDocument();
    run.replace(8);
    const answered: string[] = [];
    const retired: string[] = [];
    const bind = (name: string, document: FakeWelcomeDocument, url: string): void =>
      router.bind({
        document,
        url,
        answer: async (_request, generation) => {
          answered.push(`${name} ${generation}`);
          return name;
        },
        retire: async generation => {
          retired.push(`${name} ${generation}`);
        },
      });
    bind('main', main, 'app://vibesys/welcome.html');
    bind('run', run, 'app://vibesys/shell.html');
    const ask = (sender: number, url: string, generation: string) =>
      ipc.invokeFromSender(sender, url, generation, WELCOME_CHANNELS.releaseLogs);

    await expect(ask(7, 'app://vibesys/welcome.html', '10:100')).resolves.toEqual({
      ok: true,
      value: 'main',
    });
    await expect(ask(8, 'app://vibesys/shell.html?run=x', '20:200')).resolves.toEqual({
      ok: true,
      value: 'run',
    });
    // A sender that is neither window's current document, or on another page, is refused.
    await expect(ask(9, 'app://vibesys/welcome.html', '30:300')).resolves.toMatchObject({
      ok: false,
    });
    await expect(ask(8, 'app://vibesys/welcome.html', '20:200')).resolves.toMatchObject({
      ok: false,
    });

    // Closing the run window retires only its documents; the main window still answers.
    run.destroy();
    await spin();
    expect(retired).toEqual(['run 20:200']);
    await expect(ask(8, 'app://vibesys/welcome.html', '20:200')).resolves.toMatchObject({
      ok: false,
      error: 'not the welcome view',
    });
    await expect(ask(7, 'app://vibesys/welcome.html', '10:100')).resolves.toEqual({
      ok: true,
      value: 'main',
    });
    expect(answered).toEqual(['main 10:100', 'run 20:200', 'main 10:100']);
  });
});
