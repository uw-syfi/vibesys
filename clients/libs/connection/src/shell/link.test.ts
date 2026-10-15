import {describe, expect, test} from 'bun:test';
import {FakeClock} from '@vibesys/transport/testing';
import {ConnectionManager, SshLink} from '../index.js';
import {deferredKillProcess, FakeHelper, FakeLink, fakeProcess} from '../testing/index.js';

type Spawn = NonNullable<ConstructorParameters<typeof SshLink>[0]['spawn']>;

function sshLink(spawn: Spawn, clock = new FakeClock()): SshLink {
  return new SshLink({
    alias: 'gpu',
    controlPath: '/tmp/cm-%C',
    askpass: '/askpass',
    spawn,
    scheduleTimeout: clock.scheduleTimeout,
    retirementCommandTimeoutMs: 100,
  });
}

function operation(args: readonly string[]): string | null {
  const control = args.indexOf('-O');
  return control < 0 ? null : (args[control + 1] ?? null);
}

function completed(code = 0) {
  const process = fakeProcess();
  process.end(code);
  return process.process;
}

async function rejected(promise: Promise<unknown>): Promise<Error> {
  try {
    await promise;
  } catch (error) {
    return error as Error;
  }
  throw new Error('resolved instead of rejecting');
}

describe('SshLink interactive admission', () => {
  for (const scenario of [
    {name: 'background then interactive', callers: [false, true], interactive: true},
    {name: 'interactive then background', callers: [true, false], interactive: true},
    {name: 'background only', callers: [false, false], interactive: false},
  ] as const) {
    test(`${scenario.name} callers share one master with their combined live intent`, async () => {
      const check = fakeProcess();
      const master = fakeProcess();
      const masters: {args: readonly string[]; env: Readonly<Record<string, string>>}[] = [];
      let announceMaster = (): void => {};
      const masterStarted = new Promise<void>(resolve => {
        announceMaster = resolve;
      });
      const spawn: Spawn = (_command, args, env) => {
        if (operation(args) === 'check') return check.process;
        if (operation(args) === 'exit') return completed();
        masters.push({args, env});
        announceMaster();
        return master.process;
      };
      const link = sshLink(spawn);
      const waiters = scenario.callers.map(interactive => link.establish(interactive));

      check.end(255);
      await masterStarted;

      expect(masters).toHaveLength(1);
      expect(masters[0]?.args.includes('BatchMode=yes')).toBe(!scenario.interactive);
      expect(masters[0]?.env).toEqual(
        scenario.interactive ? {SSH_ASKPASS: '/askpass', SSH_ASKPASS_REQUIRE: 'force'} : {},
      );
      master.end(0);
      await Promise.all(waiters);
      await link.release();
    });
  }

  test('an interactive caller retires a batch master before starting its successor', async () => {
    const clock = new FakeClock();
    const firstCheck = completed(255);
    const batchSignals: string[] = [];
    const batch = deferredKillProcess(signal => batchSignals.push(signal));
    const secondCheck = fakeProcess();
    const interactiveMaster = fakeProcess();
    let checks = 0;
    let masters = 0;
    let announceBatch = (): void => {};
    let announceSecondCheck = (): void => {};
    let announceInteractiveMaster = (): void => {};
    const batchStarted = new Promise<void>(resolve => {
      announceBatch = resolve;
    });
    const secondCheckStarted = new Promise<void>(resolve => {
      announceSecondCheck = resolve;
    });
    const interactiveMasterStarted = new Promise<void>(resolve => {
      announceInteractiveMaster = resolve;
    });
    const spawn: Spawn = (_command, args, env) => {
      if (operation(args) === 'check') {
        checks += 1;
        if (checks === 1) return firstCheck;
        announceSecondCheck();
        return secondCheck.process;
      }
      if (operation(args) === 'exit') return completed();
      masters += 1;
      if (masters === 1) {
        expect(args).toContain('BatchMode=yes');
        expect(env).toEqual({});
        announceBatch();
        return batch.process;
      }
      expect(args).not.toContain('BatchMode=yes');
      expect(env).toEqual({SSH_ASKPASS: '/askpass', SSH_ASKPASS_REQUIRE: 'force'});
      announceInteractiveMaster();
      return interactiveMaster.process;
    };
    const link = sshLink(spawn, clock);
    const background = link.establish(false);
    await batchStarted;

    const signingIn = link.establish(true);

    expect(batchSignals).toEqual(['SIGTERM']);
    expect(checks).toBe(1);
    expect(batch.process.running).toBe(true);
    batch.end(null);
    await secondCheckStarted;
    expect(checks).toBe(2);
    secondCheck.end(255);
    await interactiveMasterStarted;
    expect(masters).toBe(2);
    interactiveMaster.end(0);
    await Promise.all([background, signingIn]);
    await link.release();
  });

  test('a reentrant promotion owns the spawned batch child before admitting its successor', async () => {
    const firstCheck = completed(255);
    const batchSignals: string[] = [];
    const batch = deferredKillProcess(signal => batchSignals.push(signal));
    const secondCheck = fakeProcess();
    const interactiveMaster = fakeProcess();
    let link: SshLink;
    let signingIn: Promise<void> | null = null;
    let checks = 0;
    let masters = 0;
    let announceBatch = (): void => {};
    let announceSecondCheck = (): void => {};
    let announceInteractiveMaster = (): void => {};
    const batchStarted = new Promise<void>(resolve => {
      announceBatch = resolve;
    });
    const secondCheckStarted = new Promise<void>(resolve => {
      announceSecondCheck = resolve;
    });
    const interactiveMasterStarted = new Promise<void>(resolve => {
      announceInteractiveMaster = resolve;
    });
    const spawn: Spawn = (_command, args) => {
      if (operation(args) === 'check') {
        checks += 1;
        if (checks === 1) return firstCheck;
        announceSecondCheck();
        return secondCheck.process;
      }
      if (operation(args) === 'exit') return completed();
      masters += 1;
      if (masters === 1) {
        signingIn = link.establish(true);
        announceBatch();
        return batch.process;
      }
      announceInteractiveMaster();
      return interactiveMaster.process;
    };
    link = sshLink(spawn);

    const background = link.establish(false);
    await batchStarted;

    expect(batchSignals).toEqual(['SIGTERM']);
    expect(checks).toBe(1);
    batch.end(null);
    await secondCheckStarted;
    secondCheck.end(255);
    await interactiveMasterStarted;
    interactiveMaster.end(0);
    const promoted = signingIn;
    if (promoted === null) throw new Error('the batch launch did not re-enter sign-in');
    await Promise.all([background, promoted]);
    await link.release();
  });

  test('a release re-entering the first spawn owns that child before it can create a master', async () => {
    const checkSignals: string[] = [];
    const check = deferredKillProcess(signal => checkSignals.push(signal));
    let link: SshLink;
    let releasing: Promise<void> | null = null;
    let releaseCompleted = false;
    let retirementCommands = 0;
    let masters = 0;
    const spawn: Spawn = (_command, args) => {
      const control = operation(args);
      if (control === 'check') {
        releasing = link.release();
        void releasing.then(() => {
          releaseCompleted = true;
        });
        return check.process;
      }
      if (control === 'exit') {
        retirementCommands += 1;
        return completed();
      }
      masters += 1;
      return completed();
    };
    link = sshLink(spawn);

    const establishing = link.establish(false);
    const release = releasing;
    if (release === null) throw new Error('the check spawn did not re-enter release');
    await Promise.resolve();
    await Promise.resolve();

    expect(checkSignals).toEqual(['SIGTERM']);
    expect(check.process.running).toBe(true);
    expect(releaseCompleted).toBe(false);
    expect(retirementCommands).toBe(0);
    expect(masters).toBe(0);

    check.end(null);
    expect((await rejected(establishing)).message).toContain('abandoned');
    await release;

    expect(releaseCompleted).toBe(true);
    expect(retirementCommands).toBe(1);
    expect(masters).toBe(0);
    expect((await rejected(link.establish(true))).message).toContain('released');
  });

  test('an unconfirmed batch retirement rejects promotion without starting a successor', async () => {
    const clock = new FakeClock();
    const check = completed(255);
    const batch = deferredKillProcess();
    let checks = 0;
    let announceBatch = (): void => {};
    const batchStarted = new Promise<void>(resolve => {
      announceBatch = resolve;
    });
    const spawn: Spawn = (_command, args) => {
      if (operation(args) === 'check') {
        checks += 1;
        return check;
      }
      if (operation(args) === 'exit') return completed();
      announceBatch();
      return batch.process;
    };
    const link = sshLink(spawn, clock);
    const background = link.establish(false);
    await batchStarted;
    const signingIn = link.establish(true);

    clock.advanceBy(200);
    const [backgroundError, signInError] = await Promise.all([
      rejected(background),
      rejected(signingIn),
    ]);

    expect(backgroundError.message).toContain('could not confirm termination');
    expect(signInError.message).toContain('could not confirm termination');
    expect(checks).toBe(1);
    batch.end(null);
    await batch.process.exit;
    await link.release();
  });

  test('an interactive waiter abandoned before master launch cannot authorize a prompt', async () => {
    const check = fakeProcess();
    const master = fakeProcess();
    let masterArgs: readonly string[] = [];
    let masterEnv: Readonly<Record<string, string>> = {};
    let announceMaster = (): void => {};
    const masterStarted = new Promise<void>(resolve => {
      announceMaster = resolve;
    });
    const spawn: Spawn = (_command, args, env) => {
      if (operation(args) === 'check') return check.process;
      if (operation(args) === 'exit') return completed();
      masterArgs = args;
      masterEnv = env;
      announceMaster();
      return master.process;
    };
    const link = sshLink(spawn);
    const background = link.establish(false);
    const controller = new AbortController();
    const signingIn = link.establish(true, controller.signal);

    controller.abort();
    expect((await rejected(signingIn)).message).toContain('abandoned');
    check.end(255);
    await masterStarted;

    expect(masterArgs).toContain('BatchMode=yes');
    expect(masterEnv).toEqual({});
    master.end(0);
    await background;
    await link.release();
  });

  test('manager sign-in promotes background readiness to an interactive master', async () => {
    const clock = new FakeClock();
    const helper = new FakeHelper();
    const check = fakeProcess();
    const master = fakeProcess();
    const masters: {args: readonly string[]; env: Readonly<Record<string, string>>}[] = [];
    let announceCheck = (): void => {};
    let announceMaster = (): void => {};
    const checkStarted = new Promise<void>(resolve => {
      announceCheck = resolve;
    });
    const masterStarted = new Promise<void>(resolve => {
      announceMaster = resolve;
    });
    const spawn: Spawn = (_command, args, env) => {
      if (operation(args) === 'check') {
        announceCheck();
        return check.process;
      }
      if (operation(args) === 'exit') return completed();
      if (args.at(-1) === 'true') {
        masters.push({args, env});
        announceMaster();
        return master.process;
      }
      return helper.spawn();
    };
    const link = sshLink(spawn, clock);
    const manager = new ConnectionManager({
      linkFor: () => link,
      appVersion: 'test',
      scheduleTimeout: clock.scheduleTimeout,
      log: () => {},
    });
    const lease = await manager.acquire({kind: 'ssh', alias: 'gpu'}, '/src/vibesys');
    const ready = lease.connection.ready();
    await checkStarted;

    const signingIn = manager.signIn({kind: 'ssh', alias: 'gpu'});
    check.end(255);
    await masterStarted;

    expect(masters).toHaveLength(1);
    expect(masters[0]?.args).not.toContain('BatchMode=yes');
    expect(masters[0]?.env).toEqual({
      SSH_ASKPASS: '/askpass',
      SSH_ASKPASS_REQUIRE: 'force',
    });
    master.end(0);
    await Promise.all([ready, signingIn]);
    await lease[Symbol.asyncDispose]();
    await manager[Symbol.asyncDispose]();
  });
});

describe('FakeLink establishment', () => {
  test('concurrent callers share a held attempt until its final waiter leaves', async () => {
    const link = new FakeLink();
    const pending = link.deferEstablish();
    const abandoned = new AbortController();
    const first = link.establish(false, abandoned.signal);
    const second = link.establish(true);
    await pending.received;

    abandoned.abort();

    expect((await rejected(first)).message).toContain('abandoned');
    expect(pending.abandoned).toBe(false);
    pending.release();
    await second;
    expect(link.establishments).toBe(2);
    expect(link.interactive).toBe(1);
  });

  test('release abandons a shared held attempt without stranding later fake acquisition', async () => {
    const link = new FakeLink();
    const pending = link.deferEstablish();
    const first = link.establish(false);
    const second = link.establish(true);
    await pending.received;

    const releasing = link.release();
    expect(pending.abandoned).toBe(true);
    const errors = await Promise.all([rejected(first), rejected(second)]);
    await releasing;

    expect(errors.every(error => error.message.includes('abandoned'))).toBe(true);
    expect(link.released).toBe(1);
    await link.establish(false);
    await link.release();
    expect(link.released).toBe(2);
  });
});
