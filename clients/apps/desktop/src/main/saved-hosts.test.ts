import {create, toBinary} from '@bufbuild/protobuf';
import {describe, expect, it} from '@effect/vitest';
import {DesktopHostsSchema} from '@vibesys/proto';
import * as Effect from 'effect/Effect';
import * as Schema from 'effect/Schema';
import {hostFromKey, hostKey} from '../state/host.js';
import type {ReadFile} from './files.js';
import {savedHosts} from './saved-hosts.js';

const filesystem =
  (files: Record<string, Uint8Array>): ReadFile =>
  path =>
    Effect.succeed(files[path] ?? null);

const pb = (keys: readonly string[]): Uint8Array =>
  toBinary(
    DesktopHostsSchema,
    create(DesktopHostsSchema, {
      version: 1,
      hosts: Object.fromEntries(keys.map(key => [key, {checkout: '/srv/vibesys'}])),
    }),
  );

describe('saved hosts', () => {
  it.effect('reads the host keys of the protobuf file', () =>
    Effect.gen(function* () {
      const hosts = yield* savedHosts(
        '/data',
        filesystem({'/data/hosts.pb': pb(['local', 'ssh:host-a'])}),
        () => {},
      );
      expect([...hosts].sort()).toEqual(['local', 'ssh:host-a']);
    }),
  );

  it.effect('reads the older JSON file when it is the only one, and prefers the protobuf one', () =>
    Effect.gen(function* () {
      const json = new TextEncoder().encode(
        JSON.stringify({version: 2, hosts: {'ssh:host-b': {checkout: '/srv'}}}),
      );
      expect(yield* savedHosts('/data', filesystem({'/data/hosts.json': json}), () => {})).toEqual([
        'ssh:host-b',
      ]);
      expect(
        yield* savedHosts(
          '/data',
          filesystem({'/data/hosts.json': json, '/data/hosts.pb': pb(['ssh:host-a'])}),
          () => {},
        ),
      ).toEqual(['ssh:host-a']);
    }),
  );

  it.effect('has no hosts when nothing is saved', () =>
    Effect.gen(function* () {
      expect(yield* savedHosts('/data', filesystem({}), () => {})).toEqual([]);
    }),
  );

  it.effect.prop(
    'keeps the usable keys and names each one it ignores',
    {keys: Schema.Array(Schema.String.check(Schema.isMaxLength(12))).check(Schema.isMaxLength(6))},
    ({keys}) =>
      Effect.gen(function* () {
        const lines: string[] = [];
        const json = new TextEncoder().encode(
          JSON.stringify({version: 2, hosts: Object.fromEntries(keys.map(key => [key, {}]))}),
        );
        const hosts = yield* savedHosts('/data', filesystem({'/data/hosts.json': json}), line =>
          lines.push(line),
        );
        for (const host of hosts) expect(hostKey(hostFromKey(host))).toBe(host);
        expect(hosts.length + lines.length).toBe(new Set(keys).size);
      }),
  );

  it.effect('logs a file it cannot parse and opens with no hosts', () =>
    Effect.gen(function* () {
      const lines: string[] = [];
      const hosts = yield* savedHosts(
        '/data',
        filesystem({'/data/hosts.pb': new Uint8Array([0xff, 0xff])}),
        line => lines.push(line),
      );
      expect(hosts).toEqual([]);
      expect(lines[0]).toContain('could not read the saved hosts');
    }),
  );
});
