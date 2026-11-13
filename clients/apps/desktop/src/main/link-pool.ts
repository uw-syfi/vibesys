/**
 * One ssh link per host, shared by everything that needs a channel to it, so concurrent users
 * never race two sign-ins against one control path. The link is created on the first lease and
 * released (its master ended) when the last lease ends. A host cannot be leased again until its
 * previous link has finished releasing, because two masters must never own one control path.
 *
 * A release that could not be confirmed (the master froze and could not be killed) does not refuse
 * the host for good: the next lease of that host retries the release, so a master that can be
 * ended later is redialed, and one that still cannot be is refused with the reason, for that
 * lease only.
 */
import * as Deferred from 'effect/Deferred';
import * as Effect from 'effect/Effect';
import * as Exit from 'effect/Exit';
import * as Fiber from 'effect/Fiber';
import * as Option from 'effect/Option';
import type * as Scope from 'effect/Scope';
import {LinkFailed} from './errors.js';
import {ProcessSpawner} from './process.js';
import {type MasterCell, makeSshLink, type SshLink} from './ssh-link.js';

export interface LinkPool {
  /** The host's link, held until `scope` closes. */
  readonly lease: (alias: string) => Effect.Effect<SshLink, LinkFailed, Scope.Scope>;
}

interface Entry {
  readonly link: SshLink;
  users: number;
}

/**
 * How long the pool's own close waits for one host's retirement before giving up on that host and
 * moving on to resolve. One host's teardown (`ssh-link.ts`'s `retireMaster`/`killMaster`) is a
 * chain of up to four sequentially-bounded control commands (`-O exit`, then `killMaster`'s `ps`
 * and `kill -KILL`, and a final `-O check`), each bounded by that file's own `CONTROL_COMMAND_BOUND`
 * (1 second) by default, and a lease racing to establish around the same moment can itself be
 * mid-way through that file's `CHECK_BOUND` (5 seconds). Left unbounded here, one host frozen deep
 * enough to walk that whole chain has its full total charged against the pool's own close, and so
 * against every other host's teardown and index.ts's `RELEASE_BOUND_MS`. A host that trips this
 * bound keeps retiring in the background instead of being interrupted: ending its master is still
 * worth finishing, just not worth the rest of the app's quit waiting on.
 */
export const RETIRE_BOUND_MS = 2_000;

/**
 * A pool of links under `controlPath` (a template with ssh's `%C`). Closing the scope it is made
 * in releases every link still held and waits for the releases.
 */
export const makeLinkPool = (
  controlPath: string,
  log: (line: string) => void,
  /** The ssh flags a host needs beyond its alias (a saved port, jump host or identity), asked at each new link. */
  sshArgsFor: (alias: string) => Effect.Effect<readonly string[]> = () => Effect.succeed([]),
): Effect.Effect<LinkPool, never, ProcessSpawner | Scope.Scope> =>
  Effect.gen(function* () {
    const spawner = yield* ProcessSpawner;
    const entries = new Map<string, Entry>();
    /** Releases under way. */
    const retiring = new Map<string, Deferred.Deferred<void, LinkFailed>>();
    /** Links whose release failed: the host's next lease tries them again before anything else. */
    const stuck = new Map<string, SshLink>();
    /** What each host's master reported, kept across its links. */
    const masters = new Map<string, MasterCell>();
    let closed = false;

    const masterOf = (alias: string): MasterCell => {
      let cell = masters.get(alias);
      if (cell === undefined) {
        cell = {pid: undefined};
        masters.set(alias, cell);
      }
      return cell;
    };

    const retire = (alias: string, entry: Entry) =>
      Effect.gen(function* () {
        entries.delete(alias);
        const done = Deferred.makeUnsafe<void, LinkFailed>();
        retiring.set(alias, done);
        const exit = yield* Effect.exit(entry.link.release);
        if (Exit.isFailure(exit)) {
          log(`could not retire ${alias}`);
          stuck.set(alias, entry.link);
        }
        if (retiring.get(alias) === done) retiring.delete(alias);
        yield* Deferred.done(done, exit);
      });

    /**
     * `retire(alias, entry)`, but given up on in the background past `RETIRE_BOUND_MS` instead of
     * blocking the pool's own close on it. The retirement itself is forked, not interrupted, so a
     * host that answers late (or never) still gets its master torn down once it does; only the
     * pool's own close stops waiting on it, and says which host it gave up on.
     */
    const retireBounded = (alias: string, entry: Entry) =>
      Effect.gen(function* () {
        const fiber = yield* Effect.forkDetach(retire(alias, entry));
        const joined = yield* Effect.timeoutOption(Fiber.join(fiber), `${RETIRE_BOUND_MS} millis`);
        if (Option.isNone(joined))
          log(`gave up waiting for ${alias} to release (still retiring in the background)`);
      });

    /** Wait for any earlier release of the host to finish, whether it did or not. */
    const afterRetiring = (alias: string) =>
      Effect.gen(function* () {
        for (
          let barrier = retiring.get(alias);
          barrier !== undefined;
          barrier = retiring.get(alias)
        )
          yield* Effect.exit(Deferred.await(barrier));
      });

    /**
     * The last release of the host was not confirmed: try it again. A master that still cannot be
     * ended refuses this lease with the reason, and the next lease tries once more.
     */
    const retryUnreleased = (alias: string) =>
      Effect.gen(function* () {
        const unreleased = stuck.get(alias);
        if (unreleased === undefined) return;
        yield* unreleased.release;
        stuck.delete(alias);
      });

    const lease: LinkPool['lease'] = alias =>
      Effect.gen(function* () {
        yield* afterRetiring(alias);
        if (closed)
          return yield* new LinkFailed({problem: 'link', message: 'the link pool was released'});
        yield* retryUnreleased(alias);
        let entry = entries.get(alias);
        if (entry === undefined) {
          const extraArgs = yield* sshArgsFor(alias);
          const link = yield* makeSshLink({
            alias,
            controlPath,
            master: masterOf(alias),
            extraArgs,
          }).pipe(Effect.provideService(ProcessSpawner, spawner));
          entry = {link, users: 0};
          entries.set(alias, entry);
        }
        const held = entry;
        held.users += 1;
        yield* Effect.addFinalizer(() =>
          Effect.suspend(() => {
            held.users -= 1;
            return held.users === 0 && entries.get(alias) === held
              ? retire(alias, held)
              : Effect.void;
          }),
        );
        return held.link;
      });

    yield* Effect.addFinalizer(() =>
      Effect.suspend(() => {
        closed = true;
        return Effect.forEach(
          [...entries].map(([alias, entry]) => retireBounded(alias, entry)),
          effect => effect,
          {concurrency: 'unbounded', discard: true},
        );
      }),
    );
    return {lease};
  });
