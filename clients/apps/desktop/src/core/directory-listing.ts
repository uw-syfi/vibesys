/**
 * The host's folder listings, kept by folder for the Start form's path completion. The host's own
 * `ListDirectory` answers one folder at a time; this asks for a folder once and keeps the answer
 * (or the host's refusal of it) until `forget`, so typing within a folder, going back to one, or
 * narrowing by a name never asks again. Hidden entries are a separate listing of the same folder,
 * asked for only when the name being typed starts with a dot.
 */
import * as Effect from 'effect/Effect';
import * as Ref from 'effect/Ref';
import * as Atom from 'effect/reactivity/Atom';
import * as SubscriptionRef from 'effect/SubscriptionRef';
import {type FolderListing, listingKey} from '../state/index.js';
import {faultOf} from './fault.js';
import type {HostParts} from './host-store.js';

export interface DirectoryListings {
  /** The listing of each folder asked for, by `listingKey`. */
  readonly state: Atom.Atom<ReadonlyMap<string, FolderListing>>;
  /**
   * Ask the host for `folder` unless it was asked for already. With `fresh` every listing is dropped
   * first, so the folder is read again: the form was entered afresh. Never fails: a refusal is the answer.
   */
  readonly want: (folder: string, hidden: boolean, fresh: boolean) => Effect.Effect<void>;
}

export const makeDirectoryListings = Effect.fn('DirectoryListings.make')(function* ({
  client,
}: HostParts) {
  const listings = yield* SubscriptionRef.make<ReadonlyMap<string, FolderListing>>(new Map());
  const asked = yield* Ref.make<ReadonlySet<string>>(new Set());
  const put = (key: string, listing: FolderListing) =>
    SubscriptionRef.update(listings, all => new Map(all).set(key, listing));
  const want = Effect.fn('DirectoryListings.want')(function* (
    folder: string,
    hidden: boolean,
    fresh: boolean,
  ) {
    if (fresh) {
      yield* Ref.set(asked, new Set());
      yield* SubscriptionRef.set(listings, new Map());
    }
    const key = listingKey(folder, hidden);
    const known = yield* Ref.modify(asked, (all): [boolean, ReadonlySet<string>] => [
      all.has(key),
      new Set(all).add(key),
    ]);
    if (known) return;
    yield* put(key, {_tag: 'Loading'});
    const listing = yield* client.listDirectory({path: folder, includeHidden: hidden}).pipe(
      Effect.map(
        (reply): FolderListing => ({
          _tag: 'Listed',
          entries: reply.entries,
          truncated: reply.truncated,
        }),
      ),
      Effect.catch(error =>
        Effect.succeed<FolderListing>({_tag: 'Failed', reason: faultOf(error).detail}),
      ),
    );
    yield* put(key, listing);
  });
  return {
    state: Atom.keepAlive(Atom.subscriptionRef(listings)),
    want,
  } satisfies DirectoryListings;
});
