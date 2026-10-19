/**
 * The harness and model picker of a new chat: the host's `harnesses.list` catalogue (the one New
 * run draws on) as two small choices, the provider's CLI and the model it runs. A name comes from
 * the catalogue's `display`; a pair the host did not name shows its raw ids.
 */
import type {HarnessChoice, HarnessListing, HarnessOffer} from '@vibesys/ipc';

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

/** The pair a new chat starts with: the first provider's default model. */
export function defaultChoice(offers: readonly HarnessOffer[]): HarnessChoice | null {
  const first = offers[0];
  if (first === undefined) return null;
  const preferred = offers.find(offer => offer.provider === first.provider && offer.default);
  const offer = preferred ?? first;
  return {provider: offer.provider, model: offer.model};
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

/** The pair as the catalogue names it ("Claude Code Opus"); null when the catalogue does not list it. */
export function choiceLabel(
  offers: readonly HarnessOffer[],
  choice: {readonly provider: string | null; readonly model: string | null} | null,
): string | null {
  const offer = offers.find(
    candidate => candidate.provider === choice?.provider && candidate.model === choice?.model,
  );
  return offer === undefined ? null : `${providerLabel(offer)} ${modelLabel(offer)}`;
}
