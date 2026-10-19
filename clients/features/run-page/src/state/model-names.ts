/**
 * The names a person reads for the models a run used. The host's harness catalogue (agentshim's
 * names, the ones New run shows) is the only source; a model it does not name reads as its id.
 */
import type {HarnessListing} from '@vibesys/ipc';

/** A model id to its display name, for the models the host's catalogue names. */
export type ModelNames = ReadonlyMap<string, string>;

export const NO_MODEL_NAMES: ModelNames = new Map();

/** The names `listing` gives its models; none from a host whose helper offers no catalogue. */
export function modelNamesOf(listing: HarnessListing): ModelNames {
  if (listing.kind !== 'listed') return NO_MODEL_NAMES;
  return new Map(
    listing.harnesses.flatMap(({model, display}) =>
      display?.modelName === undefined ? [] : [[model, display.modelName] as const],
    ),
  );
}

/** What a screen shows for `model`: its display name, else the id itself. */
export function modelLabel(names: ModelNames, model: string): string {
  return names.get(model) ?? model;
}
