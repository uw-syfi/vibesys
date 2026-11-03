import {describe, expect, it} from '@effect/vitest';
import * as Effect from 'effect/Effect';
import * as Fiber from 'effect/Fiber';
import * as Schema from 'effect/Schema';
import * as TestClock from 'effect/testing/TestClock';
import {commandOf} from '../state/remote-command.js';
import {fakeSpawner, type Started} from '../testing/main/fake-process.js';
import {LinkFailed} from './errors.js';
import {makeSshLink} from './ssh-link.js';

const OPTIONS = {alias: 'host-a', controlPath: '/tmp/vsd-1/%C'};

const isCheck = (started: Started): boolean =>
  started.args.includes('-O') && started.args.includes('check');
const isExit = (started: Started): boolean =>
  started.args.includes('-O') && started.args.includes('exit');
const isMaster = (started: Started): boolean => started.args.includes('ControlMaster=auto');

/** An ssh whose master is up or down, and what starting a master does. */
function fakeSsh(master: 'comes-up' | {readonly fails: string} | 'hangs') {
  const state = {up: false};
  return {
    state,
    spawner: fakeSpawner(started => {
      if (isCheck(started)) started.process.end(state.up ? 0 : 255, '', 'no master');
      else if (isExit(started)) {
        state.up = false;
        started.process.end(0);
      } else if (isMaster(started)) {
        if (master === 'comes-up') {
          state.up = true;
          started.process.end(0);
        } else if (master !== 'hangs') started.process.end(255, '', master.fails);
      }
    }),
  };
}

describe('ssh link establish', () => {
  it.effect('does nothing when the master is already up', () =>
    Effect.gen(function* () {
      const ssh = fakeSsh('comes-up');
      ssh.state.up = true;
      const link = yield* makeSshLink(OPTIONS).pipe(Effect.provide(ssh.spawner.layer));
      yield* link.establish;
      expect(ssh.spawner.started.filter(isMaster)).toHaveLength(0);
      expect(ssh.spawner.started.filter(isCheck)).toHaveLength(1);
    }),
  );

  it.effect('starts a batch-mode master that persists, and is up for the next caller', () =>
    Effect.gen(function* () {
      const ssh = fakeSsh('comes-up');
      const link = yield* makeSshLink(OPTIONS).pipe(Effect.provide(ssh.spawner.layer));
      yield* link.establish;
      const master = ssh.spawner.started.find(isMaster);
      expect(master?.command).toBe('ssh');
      expect(master?.args).toEqual(
        expect.arrayContaining([
          'ControlPath=/tmp/vsd-1/%C',
          'BatchMode=yes',
          'ControlPersist=30m',
          'ConnectTimeout=20',
          'host-a',
        ]),
      );
      expect(master?.args.at(-2)).toBe('host-a');
      yield* link.establish;
      expect(ssh.spawner.started.filter(isMaster)).toHaveLength(1);
    }),
  );

  it.effect('serializes callers so that two of them start one master', () =>
    Effect.gen(function* () {
      const ssh = fakeSsh('comes-up');
      const link = yield* makeSshLink(OPTIONS).pipe(Effect.provide(ssh.spawner.layer));
      yield* Effect.all([link.establish, link.establish, link.establish], {
        concurrency: 'unbounded',
      });
      expect(ssh.spawner.started.filter(isMaster)).toHaveLength(1);
    }),
  );

  it.effect('words a refusal by who can answer it', () =>
    Effect.gen(function* () {
      const cases: ReadonlyArray<readonly [string, 'auth' | 'capacity' | 'link']> = [
        ['host-a: Permission denied (publickey).', 'auth'],
        ['Host key verification failed.', 'auth'],
        [
          'mux_client_request_session: session request failed: Session open refused by peer',
          'capacity',
        ],
        ['channel 2: open failed: administratively prohibited', 'capacity'],
        ['ssh: Could not resolve hostname host-a', 'link'],
      ];
      for (const [stderr, problem] of cases) {
        const ssh = fakeSsh({fails: stderr});
        const link = yield* makeSshLink(OPTIONS).pipe(Effect.provide(ssh.spawner.layer));
        const failure = yield* Effect.flip(link.establish);
        expect(failure).toBeInstanceOf(LinkFailed);
        expect(failure.problem).toBe(problem);
        expect(failure.message).toContain(stderr.split('\n').at(-1) ?? '');
      }
    }),
  );

  it.effect.prop(
    'names the last line ssh wrote, whatever else it said',
    {
      lines: Schema.Array(Schema.String.check(Schema.isPattern(/^[A-Za-z]{1,20}$/))).check(
        Schema.isMinLength(1),
        Schema.isMaxLength(4),
      ),
    },
    ({lines}) =>
      Effect.gen(function* () {
        const ssh = fakeSsh({fails: lines.join('\n')});
        const link = yield* makeSshLink(OPTIONS).pipe(Effect.provide(ssh.spawner.layer));
        const failure = yield* Effect.flip(link.establish);
        expect(failure.message).toContain(lines.at(-1) ?? '');
      }),
  );

  it.effect('retires the attempt in flight when its caller is interrupted', () =>
    Effect.gen(function* () {
      const ssh = fakeSsh('hangs');
      const link = yield* makeSshLink(OPTIONS).pipe(Effect.provide(ssh.spawner.layer));
      const caller = yield* Effect.forkChild(link.establish);
      yield* Effect.yieldNow;
      yield* Effect.yieldNow;
      const master = ssh.spawner.started.find(isMaster);
      expect(master?.process.signals).toEqual([]);
      yield* Fiber.interrupt(caller);
      expect(master?.process.signals).toEqual(['SIGTERM']);
    }),
  );

  it.effect('says a start that cannot run ssh is a link failure', () =>
    Effect.gen(function* () {
      const spawner = fakeSpawner(() => 'refuse');
      const link = yield* makeSshLink(OPTIONS).pipe(Effect.provide(spawner.layer));
      const failure = yield* Effect.flip(link.establish);
      expect(failure.problem).toBe('link');
      expect(failure.message).toContain('could not start ssh');
    }),
  );
});

describe('ssh link release', () => {
  it.effect('exits the master once, however many callers release, and refuses new attempts', () =>
    Effect.gen(function* () {
      const ssh = fakeSsh('comes-up');
      const link = yield* makeSshLink(OPTIONS).pipe(Effect.provide(ssh.spawner.layer));
      yield* link.establish;
      yield* Effect.all([link.release, link.release], {concurrency: 'unbounded'});
      yield* link.release;
      expect(ssh.spawner.started.filter(isExit)).toHaveLength(1);
      expect(ssh.state.up).toBe(false);
      const failure = yield* Effect.flip(link.establish);
      expect(failure.message).toContain('released');
    }),
  );

  it.effect('abandons an attempt in flight and then exits the master', () =>
    Effect.gen(function* () {
      const ssh = fakeSsh('hangs');
      const link = yield* makeSshLink(OPTIONS).pipe(Effect.provide(ssh.spawner.layer));
      const caller = yield* Effect.forkChild(Effect.flip(link.establish));
      yield* Effect.yieldNow;
      yield* Effect.yieldNow;
      yield* link.release;
      const failure = yield* Fiber.join(caller);
      expect(failure.message).toContain('abandoned');
      const master = ssh.spawner.started.find(isMaster);
      expect(master?.process.signals).toEqual(['SIGTERM']);
      expect(ssh.spawner.started.filter(isExit)).toHaveLength(1);
    }),
  );

  it.effect('fails, and says so to every caller, when the master stays reachable', () =>
    Effect.gen(function* () {
      const spawner = fakeSpawner(started => {
        started.process.end(isExit(started) ? 255 : 0);
      });
      const link = yield* makeSshLink(OPTIONS).pipe(Effect.provide(spawner.layer));
      const first = yield* Effect.flip(link.release);
      expect(first.message).toContain('remained reachable');
      expect(yield* Effect.flip(link.release)).toBe(first);
    }),
  );

  it.effect('cannot confirm a teardown whose control command never ends', () =>
    Effect.gen(function* () {
      const spawner = fakeSpawner(() => {}, 'ignores');
      const link = yield* makeSshLink(OPTIONS).pipe(Effect.provide(spawner.layer));
      const releasing = yield* Effect.forkChild(Effect.flip(link.release));
      // The command bound, then the retire of the command itself (twice: in the body and in the
      // scope), each a TERM grace and a KILL grace.
      for (let second = 0; second < 8; second++) {
        yield* Effect.yieldNow;
        yield* TestClock.adjust('1 second');
      }
      const failure = yield* Fiber.join(releasing);
      expect(failure.message).toContain('could not confirm teardown');
      const exit = spawner.started.find(isExit);
      expect(exit?.process.signals.slice(0, 2)).toEqual(['SIGTERM', 'SIGKILL']);
    }),
  );
});

describe('ssh link channels', () => {
  it.effect('run the script through the master without prompting, and end with the scope', () =>
    Effect.gen(function* () {
      const ssh = fakeSsh('comes-up');
      const link = yield* makeSshLink(OPTIONS).pipe(Effect.provide(ssh.spawner.layer));
      yield* Effect.scoped(
        Effect.gen(function* () {
          yield* link.channel('uname -s; uname -m');
          const channel = ssh.spawner.started.at(-1);
          expect(channel?.args).toEqual(
            expect.arrayContaining(['ControlMaster=no', 'BatchMode=yes', 'host-a', '-T']),
          );
          expect(commandOf(channel?.args.at(-1) ?? '')).toBe('uname -s; uname -m');
          expect(channel?.process.signals).toEqual([]);
        }),
      );
      expect(ssh.spawner.started.at(-1)?.process.signals).toEqual(['SIGTERM']);
    }),
  );

  it.effect('explains an end by the words of the link', () =>
    Effect.gen(function* () {
      const link = yield* makeSshLink(OPTIONS).pipe(Effect.provide(fakeSpawner(() => {}).layer));
      expect(link.explain(255, 'x\nPermission denied (publickey).', 'the gateway')).toMatchObject({
        problem: 'auth',
      });
      expect(link.explain(null, '', 'the gateway').message).toContain('broke');
      expect(link.explain(3, 'boom\n', 'the gateway').message).toBe(
        'the gateway exited with 3: boom',
      );
    }),
  );
});
