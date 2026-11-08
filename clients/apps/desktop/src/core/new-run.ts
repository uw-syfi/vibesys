/**
 * What the New run screen asks of a host: its catalogue (with the host's verdict on fit), its eval
 * places, and the making of a repository from an example. Each answer is kept as the host gave it;
 * a read that starts leaves the previous answer in place (`Remote`), so the screen dims it and swaps
 * once. Starting from an example is one step for the person: the repository is made, then the run is
 * started in it, and the run's own progress is the command's, like any Start.
 */
import {create} from '@bufbuild/protobuf';
import {
  EmptyRepositorySchema,
  type ListCatalogueResponse,
  type ListEvalPlacesResponse,
  ProvisionRefusal,
  type ProvisionRepositoryRequest,
} from '@vibesys/orchestrator-proto';
import * as Effect from 'effect/Effect';
import * as Atom from 'effect/reactivity/Atom';
import * as SubscriptionRef from 'effect/SubscriptionRef';
import {
  failedOf,
  NOT_PROVISIONING,
  NOT_READ,
  type ProvisionProgress,
  type Remote,
  readingOf,
  readOf,
} from '../state/index.js';
import type {Commands, Intent} from './commands.js';
import type {DirectoryListings} from './directory-listing.js';
import {faultOf} from './fault.js';
import type {HostParts} from './host-store.js';
import {Ids} from './ids.js';

/** The repository to make from an example. */
export interface ProvisionExample {
  /** The catalogue entry's example id. */
  readonly example: string;
  /** The new repository's name. */
  readonly name: string;
  /** The folder it goes in (`~` allowed; the host expands it), empty for the host's own place. */
  readonly parent: string;
}

export interface NewRun {
  /** The host's catalogue; the data of the last read stays while the next is read. */
  readonly catalogue: Atom.Atom<Remote<ListCatalogueResponse>>;
  /** The host's eval places, with the same rule. */
  readonly places: Atom.Atom<Remote<ListEvalPlacesResponse>>;
  /** Where the repository being made stands. */
  readonly provision: Atom.Atom<ProvisionProgress>;
  /** Read the catalogue and the places (the screen was entered, or the host changed). Never fails. */
  readonly load: Effect.Effect<void>;
  /** Read the places as the host last probed them (the Machines page was opened). Never fails. */
  readonly loadPlaces: Effect.Effect<void>;
  /** Ask the host to probe its places now and read the answer. Never fails. */
  readonly refreshPlaces: Effect.Effect<void>;
  /**
   * Make `example`'s repository, then submit the run `startIn` builds for it. Answers the command's
   * id once it was submitted, or undefined when the repository could not be made (the progress says
   * why, and no run was started). Never fails.
   */
  readonly startFromExample: (
    example: ProvisionExample,
    startIn: (repository: string) => Intent,
  ) => Effect.Effect<string | undefined>;
  /**
   * Make an empty repository `name` in `parent`, then submit the setup `startIn` builds for it, with
   * the same answer as `startFromExample`.
   */
  readonly startInNewRepository: (
    repository: {readonly name: string; readonly parent: string},
    startIn: (repository: string) => Intent,
  ) => Effect.Effect<string | undefined>;
  /** Forget the outcome of the last provision (another example was picked). */
  readonly forgetProvision: Effect.Effect<void>;
}

/** One read into `ref`: the previous data stays until the answer swaps it in. */
const read = <A, E>(
  ref: SubscriptionRef.SubscriptionRef<Remote<A>>,
  ask: Effect.Effect<A, E>,
  why: (error: E) => string,
) =>
  SubscriptionRef.update(ref, readingOf).pipe(
    Effect.andThen(ask),
    Effect.flatMap(answer => SubscriptionRef.set(ref, readOf(answer))),
    Effect.catch(error => SubscriptionRef.update(ref, before => failedOf(before, why(error)))),
  );

export const makeNewRun = Effect.fn('NewRun.make')(function* (
  {client}: HostParts,
  commands: Commands,
  directories: DirectoryListings,
) {
  const ids = yield* Ids;
  const catalogue = yield* SubscriptionRef.make<Remote<ListCatalogueResponse>>(NOT_READ);
  const places = yield* SubscriptionRef.make<Remote<ListEvalPlacesResponse>>(NOT_READ);
  const provision = yield* SubscriptionRef.make<ProvisionProgress>(NOT_PROVISIONING);

  const readPlaces = (refresh: boolean) =>
    read(places, client.listEvalPlaces({refresh}), error => faultOf(error).detail);

  /** The catalogue, then the folder it names for repositories: the page counts what is in it. */
  const readCatalogue = Effect.fn('NewRun.readCatalogue')(function* () {
    yield* read(catalogue, client.listCatalogue({}), error => faultOf(error).detail);
    const folder = (yield* SubscriptionRef.get(catalogue)).data?.repositoriesDirectory ?? '';
    if (folder !== '') yield* directories.want(`${folder}/`, false, false);
  });

  const load = Effect.fn('NewRun.load')(function* () {
    yield* Effect.all([readCatalogue(), readPlaces(false)], {
      concurrency: 'unbounded',
      discard: true,
    });
  });

  const provisioned = Effect.fn('NewRun.provision')(function* (
    made: {readonly name: string; readonly parent: string},
    source: ProvisionRepositoryRequest['source'],
  ) {
    yield* SubscriptionRef.set(provision, {_tag: 'Working'});
    const requestId = yield* ids.next;
    const reply = yield* client
      .provisionRepository({
        requestId,
        name: made.name,
        source,
        parentDirectory: made.parent,
      })
      .pipe(Effect.result);
    if (reply._tag === 'Failure') {
      yield* SubscriptionRef.set(provision, {
        _tag: 'Failed',
        detail: faultOf(reply.failure).detail,
      });
      return undefined;
    }
    const {outcome} = reply.success;
    switch (outcome.case) {
      case 'provisioned': {
        const repository = outcome.value.repository;
        yield* SubscriptionRef.set(provision, {_tag: 'Done', repository});
        return repository;
      }
      case 'refused': {
        const {reason, detail, existingRepository} = outcome.value;
        yield* SubscriptionRef.set(provision, {
          _tag: 'Refused',
          reason: ProvisionRefusal[reason] ?? 'UNSPECIFIED',
          detail,
          existing: existingRepository,
        });
        return undefined;
      }
      case undefined:
        yield* SubscriptionRef.set(provision, {
          _tag: 'Failed',
          detail: 'The host sent no outcome.',
        });
        return undefined;
      default:
        outcome satisfies never;
        return undefined;
    }
  });

  const startFromExample = Effect.fn('NewRun.startFromExample')(function* (
    example: ProvisionExample,
    startIn: (repository: string) => Intent,
  ) {
    const repository = yield* provisioned(example, {case: 'example', value: example.example});
    return repository === undefined ? undefined : yield* commands.submit(startIn(repository));
  });

  const startInNewRepository = Effect.fn('NewRun.startInNewRepository')(function* (
    made: {readonly name: string; readonly parent: string},
    startIn: (repository: string) => Intent,
  ) {
    const repository = yield* provisioned(made, {
      case: 'empty',
      value: create(EmptyRepositorySchema),
    });
    return repository === undefined ? undefined : yield* commands.submit(startIn(repository));
  });

  return {
    catalogue: Atom.keepAlive(Atom.subscriptionRef(catalogue)),
    places: Atom.keepAlive(Atom.subscriptionRef(places)),
    provision: Atom.keepAlive(Atom.subscriptionRef(provision)),
    load: load(),
    loadPlaces: readPlaces(false),
    refreshPlaces: readPlaces(true),
    startFromExample,
    startInNewRepository,
    forgetProvision: SubscriptionRef.set(provision, NOT_PROVISIONING),
  } satisfies NewRun;
});
