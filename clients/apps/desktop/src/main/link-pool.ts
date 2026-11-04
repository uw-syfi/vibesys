/**
 * One ssh link per host, shared by everything that needs a channel to it, so concurrent users
 * never race two sign-ins against one control path. The link is created on the first lease and
 * released (its master ended) when the last lease ends. A host cannot be leased again until its
 * previous link has finished releasing; a release that failed leaves the master possibly still
 * owning the control path, so that host stays refused rather than run two masters on one path.
 */
import * as Deferred from 'effect/Deferred';
import * as Effect from 'effect/Effect';
import * as Exit from 'effect/Exit';
import type * as Scope from 'effect/Scope';
import {LinkFailed} from './errors.js';
import {ProcessSpawner} from './process.js';
import {makeSshLink, type SshLink} from './ssh-link.js';

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
    /** Releases under way; a failed one stays so the host is refused for good. */
    const retiring = new Map<string, Deferred.Deferred<void, LinkFailed>>();
    let closed = false;

    const retire = (alias: string, entry: Entry) =>
      Effect.gen(function* () {
        entries.delete(alias);
        const done = Deferred.makeUnsafe<void, LinkFailed>();
        retiring.set(alias, done);
        const exit = yield* Effect.exit(entry.link.release);
        if (Exit.isFailure(exit)) log(`could not retire ${alias}`);
        else if (retiring.get(alias) === done) retiring.delete(alias);
        yield* Deferred.done(done, exit);
      });

    const lease: LinkPool['lease'] = alias =>
      Effect.gen(function* () {
        // Wait for any earlier release of this host; a failed one refuses the host.
        for (
          let barrier = retiring.get(alias);
          barrier !== undefined;
          barrier = retiring.get(alias)
        )
          yield* Deferred.await(barrier);
        if (closed)
          return yield* new LinkFailed({problem: 'link', message: 'the link pool was released'});
        let entry = entries.get(alias);
        if (entry === undefined) {
          const link = yield* makeSshLink({alias, controlPath}).pipe(
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
