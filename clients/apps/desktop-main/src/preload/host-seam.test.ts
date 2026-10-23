/** A host's registry and screen use the same serialized gateway path as the run reader. */
import {expect, test} from 'bun:test';
import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import {
  ConnectionManager,
  READ_METHOD,
  registrySavedRuns,
  registrySetup,
} from '@vibesys/connection';
import {FakeLink, olderHost, phaseReached, record} from '@vibesys/connection/testing';
import {Hosts} from '@vibesys/feature-hosts/main';
import type {HostListing} from '@vibesys/hosts';
import {ReadRequestSchema, ReadResponseSchema, SavedRunImport} from '@vibesys/proto';
import {withHostSettings} from '@vibesys/settings';
import {seedsAtDepth} from '@vibesys/store/testing';
import {gatewayWorld} from '../testing/gateway-world.js';

const LOCAL = {kind: 'local'} as const;
const HASH = 'ab'.repeat(32);

test('the visible catalog equals host runs with typed refusals across concurrent calls', async () => {
  for (const seed of seedsAtDepth([1, 7, 23, 42, 97, 101, 211, 300])) {
    let count = 0;
    const runs = Array.from({length: 1 + (seed % 12)}, (_, index) => ({
      id: `run-${index}`,
      unavailable: index % 3 === 0,
    }));
    const world = gatewayWorld(seed, call => {
      count += 1;
      const method = fromBinary(ReadRequestSchema, call.bytes).method;
      if (method.case !== 'listSavedRuns') throw new Error('expected saved-run listing');
      const offset = method.value.offset;
      const page = runs.slice(offset, offset + 2);
      call.message(
        toBinary(
          ReadResponseSchema,
          create(ReadResponseSchema, {
            result: {
              case: 'listSavedRuns',
              value: {
                requestId: method.value.requestId,
                projectRoot: method.value.project,
                revision: HASH,
                nextOffset: offset + 2 < runs.length ? offset + 2 : undefined,
                runs: page.map(run => ({
                  runId: run.id,
                  displayName: run.id,
                  createdAt: {seconds: BigInt(1 + offset)},
                  imported: run.unavailable ? SavedRunImport.UNAVAILABLE : SavedRunImport.AVAILABLE,
                  detail: run.unavailable ? 'cannot convert: historical identity is absent' : '',
                })),
              },
            },
          }),
        ),
      );
      call.trailers('0', '');
    });
    const link = new FakeLink();
    const manager = new ConnectionManager({
      linkFor: () => link,
      appVersion: 'test',
      scheduleTimeout: world.clock.schedule,
      log: () => {},
    });
    const hosts = new Hosts({
      manager,
      settings: world.settings,
      localCheckout: '/checkout',
      sshConfigPath: '/missing',
      transportV2: async () => true,
      push: () => {},
      retire: () => {},
      log: () => {},
      savedRuns: registrySavedRuns({
        checkout: async () => '/checkout',
        unary: (_host, bytes, signal) => world.transport.unary(READ_METHOD, bytes, signal),
      }),
    });
    try {
      const results = await world.schedule.run(
        Promise.all([hosts.projectRuns(LOCAL, '/project'), hosts.projectRuns(LOCAL, '/project')]),
      );
      for (const listed of results) {
        expect(listed.map(run => run.runId).sort()).toEqual(runs.map(run => run.id).sort());
        for (const run of listed)
          expect(Boolean(run.unavailable)).toBe(
            Boolean(runs.find(source => source.id === run.runId)?.unavailable),
          );
      }
      await world.schedule.quiesce();
      expect(count).toBe(2 * Math.ceil(runs.length / 2));
    } finally {
      world.close();
      await manager[Symbol.asyncDispose]();
    }
  }
});

test('an older host produces the shared typed update refusal through the real bridge', async () => {
  const world = gatewayWorld(23, call => {
    void olderHost.read(LOCAL, call.bytes).then(bytes => {
      call.message(bytes);
      call.trailers('0', '');
    });
  });
  const link = new FakeLink();
  const pushed: HostListing[] = [];
  const manager = new ConnectionManager({
    linkFor: () => link,
    appVersion: 'test',
    scheduleTimeout: world.clock.schedule,
    log: () => {},
  });
  let refuse = () => {};
  const refused = new Promise<void>(resolve => {
    refuse = resolve;
  });
  await world.settings.updateHosts(current =>
    withHostSettings(current, LOCAL, {checkout: '/checkout'}),
  );
  const hosts = new Hosts({
    manager,
    settings: world.settings,
    localCheckout: '/checkout',
    sshConfigPath: '/missing',
    transportV2: async () => true,
    runs: {start: () => {}, cancel: () => {}},
    setup: registrySetup({
      unary: (_host, bytes, signal) => world.transport.unary(READ_METHOD, bytes, signal),
    }),
    push: listing => {
      pushed.push(listing);
      if (listing.kind === 'unavailable' && listing.connection.kind === 'incompatible') refuse();
    },
    retire: () => {},
    log: () => {},
  });
  try {
    await world.schedule.run(hosts.configured());
    const connection = manager.existing('local');
    if (connection === null) throw new Error('missing local connection');
    await world.schedule.run(phaseReached(connection, 'ready'));
    link.helper.publish(record('000000000001', 'run-a'));
    await world.schedule.run(refused);
    const screen = pushed.at(-1);
    expect(screen?.kind).toBe('unavailable');
    if (screen?.kind !== 'unavailable') throw new Error('expected typed refusal');
    expect(screen.connection.kind).toBe('incompatible');
    expect(JSON.stringify(screen)).not.toContain('usage:');
  } finally {
    world.close();
    await manager[Symbol.asyncDispose]();
  }
});
