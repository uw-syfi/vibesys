import {readFileSync} from 'node:fs';
import {runInThisContext} from 'node:vm';
import {expect, it} from '@effect/vitest';
import type {GatewayPortBridge} from '@vibesys/ipc';
import {GATEWAY_CHANNEL} from '@vibesys/ipc';
import * as Schema from 'effect/Schema';
import ts from 'typescript';

interface Preload {
  readonly GATEWAY_PRELOAD_CHANNEL: string;
  readonly createGatewayPreload: (capabilities: {
    channel(): {
      listen(receive: (value: unknown) => void, failed: () => void): void;
      send(request: Uint8Array): void;
      close(): void;
    };
  }) => GatewayPortBridge;
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

const {createGatewayPreload, GATEWAY_PRELOAD_CHANNEL} = loadPreload();

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
