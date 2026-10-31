/**
 * New run offers the harnesses installed on the machine first and starts with the first of them.
 *
 * The generators draw from what the contracts allow, not from the code that orders: the host
 * lists its vetted pairs in any order (an older host put the installed ones first, a newer one
 * lists its catalog as is), a pair at most once, a provider's default flagged, and the machine
 * names the CLIs it has in any order, among them ones the catalog does not vet. The oracles are
 * restated here in plain loops.
 */
import {describe, expect, test} from 'bun:test';
import type {ExampleList, HelperFeature, MachineDescription} from '@vibesys/contracts/host-helper';
import {type HostKey, REQUIRED_HELPER_FEATURES} from '@vibesys/hosts';
import type {HarnessOffer, Offered} from '@vibesys/ipc';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {createActor, fromPromise, type SnapshotFrom} from 'xstate';
import {installedFirst} from './agents.js';
import {newRunMachine} from './machine.js';

const PROVIDERS = ['claude', 'codex', 'gemini', 'opencode', 'copilot', 'a-new-harness'] as const;
/** A CLI the machine has and the catalog does not vet. */
const UNVETTED = 'a-cli-nobody-vetted';

/** Shuffled in place by Fisher-Yates over `random`. */
function shuffled<T>(items: readonly T[], random: Seeded): T[] {
  const out = [...items];
  for (let at = out.length - 1; at > 0; at -= 1) {
    const other = random.int(at + 1);
    [out[at], out[other]] = [out[other] as T, out[at] as T];
  }
  return out;
}

/** A host's list: some providers, one to three models each, one flagged default, in any order. */
function catalog(random: Seeded): HarnessOffer[] {
  const providers = PROVIDERS.filter(() => random.bool());
  const chosen = providers.length === 0 ? [random.pick(PROVIDERS)] : providers;
  return shuffled(
    chosen.flatMap(provider =>
      Array.from({length: 1 + random.int(3)}, (_, index) => ({
        provider,
        model: `${provider}-model-${index}`,
        default: index === 0,
      })),
    ),
    random,
  );
}

/** The CLIs a machine names: any of the vetted providers and unvetted ones, in any order. */
function installedOn(random: Seeded): string[] {
  return shuffled(
    [...PROVIDERS, UNVETTED].filter(() => random.int(3) === 0),
    random,
  );
}

/** What the oracle says: installed offers, then the rest, each in the order given. */
function oracle(offers: readonly HarnessOffer[], installed: readonly string[]): HarnessOffer[] {
  const first: HarnessOffer[] = [];
  const rest: HarnessOffer[] = [];
  for (const offer of offers) (installed.includes(offer.provider) ? first : rest).push(offer);
  return [...first, ...rest];
}

describe('installedFirst', () => {
  test('lists the installed harnesses first, each group in the host order, whatever the host order', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const listed = catalog(random);
      const installed = installedOn(random);
      const ordered = installedFirst(listed, installed);

      // The same offers, none lost or repeated.
      expect(ordered.length, `seed ${seed}`).toBe(listed.length);
      for (const offer of listed) expect(ordered, `seed ${seed}`).toContain(offer);

      // No uninstalled offer before an installed one.
      const flags = ordered.map(offer => installed.includes(offer.provider));
      const firstUninstalled = flags.indexOf(false);
      expect(firstUninstalled === -1 || !flags.slice(firstUninstalled).includes(true)).toBe(true);

      // Within a group, the host's order.
      expect(
        ordered.filter(o => installed.includes(o.provider)),
        `seed ${seed}`,
      ).toEqual(listed.filter(o => installed.includes(o.provider)));
      expect(
        ordered.filter(o => !installed.includes(o.provider)),
        `seed ${seed}`,
      ).toEqual(listed.filter(o => !installed.includes(o.provider)));
    }
  });

  test('is right against a host that already put the installed ones first and one that did not', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const newHost = catalog(random);
      const installed = installedOn(random);
      const oldHost = oracle(newHost, installed);
      expect(installedFirst(oldHost, installed), `seed ${seed}`).toEqual(
        installedFirst(newHost, installed),
      );
      expect(installedFirst(installedFirst(newHost, installed), installed)).toEqual(
        installedFirst(newHost, installed),
      );
    }
  });

  test('leaves the host order alone when the machine has not said what it has', () => {
    for (const seed of SEEDS.slice(0, 50)) {
      const listed = catalog(new Seeded(seed));
      expect(installedFirst(listed, null)).toEqual(listed);
    }
  });
});

function described(installed: readonly string[]): Offered<MachineDescription> {
  return {
    kind: 'offered',
    value: {
      checkout: null,
      hardware: {cpu_cores: 8, gpus: [], accelerator: 'cpu'},
      docker: {reachable: true, version: '29.1'},
      harness_clis: [...installed],
      runs: {live: 0, ended_today: 0},
    },
  };
}

type Snapshot = SnapshotFrom<typeof newRunMachine>;

/** A promise settled from outside, so a test chooses which of two reads answers first. */
function deferred<T>() {
  let settle: (value: T) => void = () => undefined;
  const promise = new Promise<T>(resolve => {
    settle = resolve;
  });
  return {promise, settle};
}

/** A New run on a machine whose description and harness list the test answers, in its own order. */
function newRunOn() {
  const description = deferred<Offered<MachineDescription>>();
  const harnesses = deferred<readonly HarnessOffer[]>();
  // Only what a form with no project asks: its helper, its machine, its harnesses and examples.
  const actor = createActor(
    newRunMachine.provide({
      actors: {
        describe: fromPromise<Offered<MachineDescription>, {readonly host: HostKey}>(
          () => description.promise,
        ),
        harnesses: fromPromise<readonly HarnessOffer[], {readonly host: HostKey}>(
          () => harnesses.promise,
        ),
        features: fromPromise<readonly HelperFeature[], {readonly host: HostKey}>(
          async () => REQUIRED_HELPER_FEATURES,
        ),
        examples: fromPromise<Offered<ExampleList>, {readonly host: HostKey}>(async () => ({
          kind: 'unsupported',
        })),
      },
    }),
    {input: {machine: 'local', recent: [], moreOpen: {}}},
  );
  const until = (ready: (snapshot: Snapshot) => boolean) =>
    new Promise<void>(resolve => {
      if (ready(actor.getSnapshot())) return resolve();
      const watch = actor.subscribe(snapshot => {
        if (!ready(snapshot)) return;
        watch.unsubscribe();
        resolve();
      });
    });
  return {actor, description, harnesses, until};
}

const LOADED = {editing: {harnesses: 'ready', facts: 'ready'}} as const;

describe('the form starts with an installed harness', () => {
  for (const order of ['the description first', 'the harness list first'] as const) {
    test(`${order}, whatever the host lists and the machine has`, async () => {
      for (const seed of SEEDS.slice(0, 120)) {
        const random = new Seeded(seed);
        const listed = catalog(random);
        const installed = installedOn(random);
        const run = newRunOn();
        run.actor.start();

        if (order === 'the description first') {
          run.description.settle(described(installed));
          await run.until(snapshot => snapshot.matches({editing: {facts: 'ready'}}));
          run.harnesses.settle(listed);
        } else {
          run.harnesses.settle(listed);
          await run.until(snapshot => snapshot.matches({editing: {harnesses: 'ready'}}));
          run.description.settle(described(installed));
        }
        await run.until(snapshot => snapshot.matches(LOADED));

        const {context} = run.actor.getSnapshot();
        const firstInstalled = listed.find(offer => installed.includes(offer.provider));
        // The first installed harness in the host's list; none installed: the host's first, as before.
        expect(context.form.agent?.provider, `seed ${seed}`).toBe(
          (firstInstalled ?? listed[0])?.provider,
        );
        expect(context.harnesses?.list, `seed ${seed}`).toEqual(oracle(listed, installed));
        expect(context.harnesses?.listed, `seed ${seed}`).toEqual(listed);
        run.actor.stop();
      }
    });
  }

  test('a machine that cannot describe itself leaves the host order and its first harness', async () => {
    for (const seed of SEEDS.slice(0, 60)) {
      const listed = catalog(new Seeded(seed));
      const run = newRunOn();
      run.actor.start();
      run.description.settle({kind: 'unsupported'});
      run.harnesses.settle(listed);
      await run.until(snapshot => snapshot.matches(LOADED));
      const {context} = run.actor.getSnapshot();
      expect(context.harnesses?.list).toEqual(listed);
      expect(context.form.agent?.provider).toBe(listed[0]?.provider);
      run.actor.stop();
    }
  });

  test('a harness the person picked before the description arrived stays, installed or not', async () => {
    for (const seed of SEEDS.slice(0, 120)) {
      const random = new Seeded(seed);
      const listed = catalog(random);
      const installed = installedOn(random);
      const run = newRunOn();
      run.actor.start();
      run.harnesses.settle(listed);
      await run.until(snapshot => snapshot.matches({editing: {harnesses: 'ready'}}));
      const picked = random.pick(listed);
      run.actor.send({type: 'agent', provider: picked.provider, model: picked.model});

      run.description.settle(described(installed));
      await run.until(snapshot => snapshot.matches(LOADED));

      expect(run.actor.getSnapshot().context.form.agent, `seed ${seed}`).toMatchObject({
        provider: picked.provider,
        model: picked.model,
      });
      run.actor.stop();
    }
  });
});
