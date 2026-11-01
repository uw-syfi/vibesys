/**
 * A read asked twice puts the same bytes on the wire. The gateway calls answer identical reads in
 * flight with one host process, so a read that stamped a fresh id on every ask would start one
 * process per screen that wants it (at a desktop launch, 3 machine descriptions and 2 harness
 * listings at once).
 */
import {describe, expect, test} from 'bun:test';
import type {HostId} from '@vibesys/hosts';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {registryRunCatalog} from './registry-catalog.js';
import {type RegistryReadPorts, registryReads} from './registry-reads.js';
import {registryRunReads} from './registry-run-reads.js';

const HOST: HostId = {kind: 'ssh', alias: 'host-a'};

/** Ports that keep each request and refuse it, since only the request's bytes are in question. */
function recording(): {ports: RegistryReadPorts; wire: Uint8Array[]} {
  const wire: Uint8Array[] = [];
  return {
    wire,
    ports: {
      checkout: async () => '/saved',
      unary: async (_host, bytes) => {
        wire.push(bytes);
        throw new Error('refused');
      },
    },
  };
}

type Ask = (ports: RegistryReadPorts, random: Seeded) => () => Promise<unknown>;

const signal = () => new AbortController().signal;
const some = (random: Seeded, prefix: string) => `${prefix}-${random.int(1000)}`;

/** Every read the registry makes that does not change a host, with arguments drawn per seed. */
const READS: Record<string, Ask> = {
  describeMachine: ports => () => registryReads(ports).describeMachine(HOST, null, signal()),
  listCompute: ports => () => registryReads(ports).listCompute(HOST, null, signal()),
  logins: ports => () => registryRunCatalog(ports).logins(HOST, signal()),
  harnesses: ports => () => registryRunCatalog(ports).harnesses(HOST, signal()),
  examples: ports => () => registryRunCatalog(ports).examples(HOST, signal()),
  tasks: (ports, random) => {
    const project = some(random, '/project');
    return () => registryRunCatalog(ports).tasks(HOST, project, signal());
  },
  taskGoal: (ports, random) => {
    const [project, task] = [some(random, '/project'), some(random, 'task')];
    return () => registryRunCatalog(ports).taskGoal(HOST, project, task, signal());
  },
  digests: (ports, random) => {
    const project = some(random, '/project');
    const runIds = Array.from({length: random.int(6)}, () => some(random, 'run'));
    return () => registryRunReads(ports).digests(HOST, project, runIds, signal());
  },
};

describe('a read asked twice', () => {
  for (const [name, ask] of Object.entries(READS)) {
    test(`${name} puts the same bytes on the wire`, async () => {
      for (const seed of SEEDS.slice(0, 20)) {
        const {ports, wire} = recording();
        const read = ask(ports, new Seeded(seed));
        await read().catch(() => {});
        await read().catch(() => {});
        expect(wire).toHaveLength(2);
        expect([...(wire[1] as Uint8Array)]).toEqual([...(wire[0] as Uint8Array)]);
      }
    });
  }
});
