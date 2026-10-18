/** Presentation choices derived only from the host's vetted model offers. */
import type {HarnessOffer} from '@vibesys/ipc';

export interface ModelTier {
  readonly rank: number;
  readonly name: string;
  readonly offers: readonly HarnessOffer[];
}

export interface ModelFamily {
  readonly name: string;
  readonly tiers: readonly ModelTier[];
}

/** Families and their tiers in ascending rank; unranked models remain in the full list. */
export function modelFamilies(offers: readonly HarnessOffer[], provider: string): ModelFamily[] {
  const families = new Map<string, Map<number, ModelTier>>();
  for (const offer of offers) {
    if (offer.provider !== provider) continue;
    const {family, tier, tierName} = offer.display ?? {};
    if (family === undefined || tier === undefined || tierName === undefined) continue;
    const tiers = families.get(family) ?? new Map<number, ModelTier>();
    const previous = tiers.get(tier);
    tiers.set(tier, {
      rank: tier,
      name: tierName,
      offers: [...(previous?.offers ?? []), offer],
    });
    families.set(family, tiers);
  }
  return [...families].map(([name, tiers]) => ({
    name,
    tiers: [...tiers.values()].sort((left, right) => left.rank - right.rank),
  }));
}

/** The host default wins when several offered model versions share a tier. */
export function modelAtTier(tier: ModelTier): string {
  const offer = tier.offers.find(each => each.default) ?? tier.offers[0];
  if (offer === undefined) throw new Error('a model tier cannot be empty');
  return offer.model;
}
