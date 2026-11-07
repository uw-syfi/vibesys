import {readFileSync} from 'node:fs';
import {runInThisContext} from 'node:vm';
import {expect, it} from '@effect/vitest';
import * as Schema from 'effect/Schema';
import ts from 'typescript';
import {GATEWAY_CHANNEL, type GatewayPortBridge} from '../core/gateway-codec.js';
import {
  PREFERENCES_READ_CHANNEL,
  PREFERENCES_WRITE_CHANNEL,
  type PreferencesBridge,
} from '../core/settings/preferences-codec.js';
import {VIEWER_READ_CHANNEL, type ViewerBridge} from '../core/viewer/viewer-codec.js';

interface Preload {
  readonly GATEWAY_PRELOAD_CHANNEL: string;
  readonly createGatewayPreload: (capabilities: {
    channel(): {
      listen(receive: (value: unknown) => void, failed: () => void): void;
      send(request: Uint8Array): void;
      close(): void;
    };
  }) => GatewayPortBridge;
  readonly PREFERENCES_PRELOAD_CHANNELS: {readonly read: string; readonly write: string};
  readonly createPreferencesPreload: (ipc: {
    invoke(channel: string, ...args: unknown[]): Promise<unknown>;
  }) => PreferencesBridge;
  readonly VIEWER_PRELOAD_CHANNEL: string;
  readonly createViewerPreload: (ipc: {
    invoke(channel: string, ...args: unknown[]): Promise<unknown>;
  }) => ViewerBridge;
}

/**
 * The real preload source, compiled in memory and run as CommonJS with no renderer process: the
 * wiring at the end of the file is skipped (`process.type` is not `renderer`), so only the
 * factory it exports runs. A Vite test cannot import a `.cts` file itself.
 */
function loadPreload(): Preload {
  const source = readFileSync(new URL('./index.cts', import.meta.url), 'utf8');
  const {outputText} = ts.transpileModule(source, {
    compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022},
  });
  const run = runInThisContext(
    `(function (module, exports, process, require) {${outputText}\n})`,
  ) as (
    module: {exports: unknown},
    exports: unknown,
    process: {type?: string},
    require: (id: string) => never,
  ) => void;
  const module = {exports: {}};
  run(module, module.exports, {}, () => {
    throw new Error('the preload must not load a module here');
  });
  return module.exports as Preload;
}

const {
  createGatewayPreload,
  GATEWAY_PRELOAD_CHANNEL,
  createPreferencesPreload,
  PREFERENCES_PRELOAD_CHANNELS,
  createViewerPreload,
  VIEWER_PRELOAD_CHANNEL,
} = loadPreload();

class Channel {
  received: (value: unknown) => void = () => {};
  failed: () => void = () => {};
  request: Uint8Array | undefined;
  closes = 0;
  sendFailure: Error | undefined;
  listen(receive: (value: unknown) => void, failed: () => void) {
    this.received = receive;
    this.failed = failed;
  }
  send(request: Uint8Array) {
    if (this.sendFailure !== undefined) throw this.sendFailure;
    this.request = request.slice();
  }
  close() {
    this.closes += 1;
  }
}
const REQUEST = Uint8Array.of(1, 2, 3);
const REPLY = Uint8Array.of(9);
const outcome = Schema.Literals(['cancel', 'delivery', 'object', 'send', 'bytes']);

it.prop(
  'every way a call can end releases its channel once, and nothing is delivered after',
  {how: outcome},
  ({how}) => {
    const channel = new Channel();
    const received: Uint8Array[] = [];
    const errors: string[] = [];
    const bridge = createGatewayPreload({channel: () => channel});
    if (how === 'send') {
      channel.sendFailure = new Error('send failed');
      expect(() =>
        bridge.open(
          REQUEST,
          value => received.push(value),
          error => errors.push(error),
        ),
      ).toThrow('send failed');
    } else {
      const call = bridge.open(
        REQUEST,
        value => received.push(value),
        error => errors.push(error),
      );
      expect(channel.request).toEqual(REQUEST);
      if (how === 'cancel') call.cancel();
      else if (how === 'delivery') channel.failed();
      else if (how === 'object') channel.received({bytes: REPLY});
      else {
        channel.received(REPLY);
        call.cancel();
      }
      channel.received(REPLY);
      channel.failed();
      call.cancel();
    }
    expect(channel.closes).toBe(1);
    expect(received).toEqual(how === 'bytes' ? [REPLY] : []);
    expect(errors).toHaveLength(how === 'object' || how === 'delivery' ? 1 : 0);
  },
);

it('the production channel name is the one main serves', () => {
  expect(GATEWAY_PRELOAD_CHANNEL).toBe(GATEWAY_CHANNEL);
});

it('the preferences channel names are the ones main serves', () => {
  expect(PREFERENCES_PRELOAD_CHANNELS).toEqual({
    read: PREFERENCES_READ_CHANNEL,
    write: PREFERENCES_WRITE_CHANNEL,
  });
});

/** An `invoke` that answers each call as the test says, and records what it was asked. */
function invoking(answer: (channel: string) => Promise<unknown>) {
  const asked: {channel: string; args: unknown[]}[] = [];
  return {
    asked,
    ipc: {
      invoke: (channel: string, ...args: unknown[]) => {
        asked.push({channel, args});
        return answer(channel);
      },
    },
  };
}

const settled = () => new Promise<void>(resolve => queueMicrotask(resolve));

it('a preferences read ends in exactly one callback: bytes, nothing, or a failure', async () => {
  for (const [reply, expected] of [
    [Promise.resolve(REPLY), {bytes: REPLY, errors: 0}],
    [Promise.resolve(null), {bytes: null, errors: 0}],
    [Promise.resolve('text'), {bytes: undefined, errors: 1}],
    [Promise.reject(new Error('denied')), {bytes: undefined, errors: 1}],
  ] as const) {
    const {ipc, asked} = invoking(() => reply);
    const got: (Uint8Array | null)[] = [];
    const errors: string[] = [];
    createPreferencesPreload(ipc).read(
      bytes => got.push(bytes),
      error => errors.push(error),
    );
    await settled();
    await settled();
    expect(asked.map(call => call.channel)).toEqual([PREFERENCES_READ_CHANNEL]);
    expect(got).toEqual(expected.bytes === undefined ? [] : [expected.bytes]);
    expect(errors).toHaveLength(expected.errors);
  }
});

it('a preferences write sends the bytes on the write channel and ends in one callback', async () => {
  const sent = invoking(() => Promise.resolve(undefined));
  let done = 0;
  createPreferencesPreload(sent.ipc).write(
    REQUEST,
    () => {
      done += 1;
    },
    () => {
      done += 100;
    },
  );
  await settled();
  expect(sent.asked).toEqual([{channel: PREFERENCES_WRITE_CHANNEL, args: [REQUEST]}]);
  expect(done).toBe(1);
  const refused = invoking(() => Promise.reject(new Error('refused')));
  const errors: string[] = [];
  createPreferencesPreload(refused.ipc).write(
    REQUEST,
    () => undefined,
    error => errors.push(error),
  );
  await settled();
  await settled();
  expect(errors).toEqual(['refused']);
});

it('the viewer channel name is the one main serves', () => {
  expect(VIEWER_PRELOAD_CHANNEL).toBe(VIEWER_READ_CHANNEL);
});

it('a viewer read ends in exactly one callback: bytes, or a failure', async () => {
  for (const [reply, expected] of [
    [Promise.resolve(REPLY), {bytes: REPLY, errors: 0}],
    [Promise.resolve(null), {bytes: undefined, errors: 1}],
    [Promise.resolve('text'), {bytes: undefined, errors: 1}],
    [Promise.reject(new Error('denied')), {bytes: undefined, errors: 1}],
  ] as const) {
    const {ipc, asked} = invoking(() => reply);
    const got: Uint8Array[] = [];
    const errors: string[] = [];
    createViewerPreload(ipc).read(
      bytes => got.push(bytes),
      error => errors.push(error),
    );
    await settled();
    await settled();
    expect(asked.map(call => call.channel)).toEqual([VIEWER_READ_CHANNEL]);
    expect(got).toEqual(expected.bytes === undefined ? [] : [expected.bytes]);
    expect(errors).toHaveLength(expected.errors);
  }
});
