/**
 * The harness and model picker of a new chat: the host's `harnesses.list` catalogue (the one New
 * run draws on) as two small choices, the provider's CLI and the model it runs. A name comes from
 * the catalogue's `display`; a pair the host did not name shows its raw ids.
 */
import type {HarnessChoice, HarnessListing, HarnessOffer} from '@vibesys/ipc';
import type {CoreState} from '@vibesys/run-state';

export interface PickerOption {
  readonly value: string;
  readonly label: string;
}

/** The catalogue as the panel holds it: loading, listed, or nothing to choose from. */
export type Catalogue =
  | {readonly kind: 'loading'}
  | {readonly kind: 'listed'; readonly offers: readonly HarnessOffer[]}
  | {readonly kind: 'none'};

export function catalogueOf(listing: HarnessListing): Catalogue {
  return listing.kind === 'listed' && listing.harnesses.length > 0
    ? {kind: 'listed', offers: listing.harnesses}
    : {kind: 'none'};
}

function providerLabel(offer: HarnessOffer): string {
  return offer.display?.providerName ?? offer.provider;
}

function modelLabel(offer: HarnessOffer): string {
  return offer.display?.modelName ?? offer.model;
}

/** The providers the catalogue offers, in its order, each once. */
export function providerOptions(offers: readonly HarnessOffer[]): readonly PickerOption[] {
  const seen = new Map<string, PickerOption>();
  for (const offer of offers) {
    if (!seen.has(offer.provider)) {
      seen.set(offer.provider, {value: offer.provider, label: providerLabel(offer)});
    }
  }
  return [...seen.values()];
}

/** The models `provider` runs, in the catalogue's order. */
export function modelOptions(
  offers: readonly HarnessOffer[],
  provider: string,
): readonly PickerOption[] {
  return offers
    .filter(offer => offer.provider === provider)
    .map(offer => ({value: offer.model, label: modelLabel(offer)}));
}

function offered(offers: readonly HarnessOffer[], choice: HarnessChoice): boolean {
  return offers.some(offer => offer.provider === choice.provider && offer.model === choice.model);
}

/**
 * The pair a new chat starts with: the first of `preferred` (the chat's last thread, then the
 * run's own harness and model) the catalogue offers, else the first provider's default model.
 */
export function defaultChoice(
  offers: readonly HarnessOffer[],
  preferred: readonly (HarnessChoice | null)[] = [],
): HarnessChoice | null {
  const first = offers[0];
  if (first === undefined) return null;
  for (const choice of preferred) {
    if (choice !== null && offered(offers, choice)) {
      return {provider: choice.provider, model: choice.model};
    }
  }
  const fallback = offers.find(offer => offer.provider === first.provider && offer.default);
  const offer = fallback ?? first;
  return {provider: offer.provider, model: offer.model};
}

/** The planner's role id: its harness and model stand for the run when roles differ. */
const PLANNER_ROLE = 'planner';

/**
 * The harness and model the run's agents ran on, from the executions its journal started: the
 * planner's when roles differ, else the first execution's. Null before any execution named both.
 */
export function runHarness(core: Pick<CoreState, 'executionStarts'>): HarnessChoice | null {
  const named = Object.values(core.executionStarts).flatMap(start =>
    start.harness !== null && start.model !== null
      ? [{role: start.role, provider: start.harness, model: start.model}]
      : [],
  );
  const chosen = named.find(start => start.role === PLANNER_ROLE) ?? named[0];
  return chosen === undefined ? null : {provider: chosen.provider, model: chosen.model};
}

/** The pair after the user picks `provider`: its default model, or the one already chosen if it runs there. */
export function withProvider(
  offers: readonly HarnessOffer[],
  current: HarnessChoice | null,
  provider: string,
): HarnessChoice | null {
  const models = offers.filter(offer => offer.provider === provider);
  const kept = models.find(offer => offer.model === current?.model);
  const offer = kept ?? models.find(candidate => candidate.default) ?? models[0];
  return offer === undefined ? current : {provider: offer.provider, model: offer.model};
}
