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
 * A pool of links under `controlPath` (a template with ssh's `%C`). Closing the scope it is made
 * in releases every link still held and waits for the releases.
 */
export const makeLinkPool = (
  controlPath: string,
  log: (line: string) => void,
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

    const lease: LinkPool['lease'] = alias =>
      Effect.gen(function* () {
        // Wait for any earlier release of this host to finish, whether it did or not.
        for (
          let barrier = retiring.get(alias);
          barrier !== undefined;
          barrier = retiring.get(alias)
        )
          yield* Effect.exit(Deferred.await(barrier));
        if (closed)
          return yield* new LinkFailed({problem: 'link', message: 'the link pool was released'});
        const unreleased = stuck.get(alias);
        if (unreleased !== undefined) {
          // The last release was not confirmed: try it again; a master that still cannot be ended
          // refuses this lease with the reason, and the next lease tries once more.
          yield* unreleased.release;
          stuck.delete(alias);
        }
        let entry = entries.get(alias);
        if (entry === undefined) {
          const link = yield* makeSshLink({alias, controlPath, master: masterOf(alias)}).pipe(
            Effect.provideService(ProcessSpawner, spawner),
          );
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
          [...entries].map(([alias, entry]) => retire(alias, entry)),
          effect => effect,
          {concurrency: 'unbounded', discard: true},
        );
      }),
    );
    return {lease};
  });
