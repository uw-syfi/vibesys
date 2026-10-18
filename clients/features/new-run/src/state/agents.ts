/**
 * The agent the host offers, as the Agent question reads it: model lines (cards), the harnesses
 * that run one, and the models of a line from smaller and cheaper to stronger (a slider). It reads
 * only what each offer's `display` says today (family, tier); a model line the host does not rank
 * is a flat list. This module is the one place that knows which display fields exist, so a host
 * that also reports makers and generations changes it and nothing else.
 */
import type {HarnessOffer} from '@vibesys/ipc';
import {harnessLabel} from './form.js';

/** One card of the Agent question: a model line and the harnesses that run it. */
export interface AgentGroup {
  readonly id: string;
  readonly name: string;
  /** The harnesses with at least one model of the line, in the host's order. */
  readonly providers: readonly string[];
}

export interface ModelTier {
  readonly rank: number;
  readonly name: string;
  readonly offers: readonly HarnessOffer[];
}

/** The line an offer belongs to: its family when the host names one, else its harness alone. */
function groupIdOf(offer: HarnessOffer): string {
  const family = offer.display?.family;
  return family === undefined ? `harness:${offer.provider}` : `family:${family}`;
}

/** The model lines `offers` make, in the order the host first lists them. */
export function agentGroups(offers: readonly HarnessOffer[]): AgentGroup[] {
  const groups = new Map<string, {name: string; providers: string[]}>();
  for (const offer of offers) {
    const id = groupIdOf(offer);
    const group = groups.get(id) ?? {
      name: offer.display?.family ?? harnessLabel(offers, offer.provider),
      providers: [],
    };
    if (!group.providers.includes(offer.provider)) group.providers.push(offer.provider);
    groups.set(id, group);
  }
  return [...groups].map(([id, group]) => ({id, ...group}));
}

/** The line of the agent `provider` and `model` name, or null when `offers` do not offer it. */
export function groupOf(
  offers: readonly HarnessOffer[],
  provider: string,
  model: string,
): AgentGroup | null {
  const offer = offers.find(each => each.provider === provider && each.model === model);
  if (offer === undefined) return null;
  return agentGroups(offers).find(group => group.id === groupIdOf(offer)) ?? null;
}

/** The offers of `group` that `provider` runs, in the host's order. */
export function offersIn(
  offers: readonly HarnessOffer[],
  group: AgentGroup,
  provider: string,
): HarnessOffer[] {
  return offers.filter(offer => offer.provider === provider && groupIdOf(offer) === group.id);
}

/**
 * The tiers of `group` on `provider`, from smallest to strongest; empty when the host does not
 * rank them or there is nothing to choose between.
 */
export function tiersIn(
  offers: readonly HarnessOffer[],
  group: AgentGroup,
  provider: string,
): ModelTier[] {
  const tiers = new Map<number, ModelTier>();
  for (const offer of offersIn(offers, group, provider)) {
    const {tier, tierName} = offer.display ?? {};
    if (tier === undefined || tierName === undefined) continue;
    const before = tiers.get(tier);
    tiers.set(tier, {rank: tier, name: tierName, offers: [...(before?.offers ?? []), offer]});
  }
  const sorted = [...tiers.values()].sort((left, right) => left.rank - right.rank);
  return sorted.length > 1 ? sorted : [];
}

/** The host's default wins when several model versions share a tier. */
export function modelAtTier(tier: ModelTier): string {
  const offer = tier.offers.find(each => each.default) ?? tier.offers[0];
  if (offer === undefined) throw new Error('a model tier cannot be empty');
  return offer.model;
}

/** The tier a model sits on, among `tiers`; null for a model off the ladder. */
export function tierOf(tiers: readonly ModelTier[], model: string): number | null {
  const index = tiers.findIndex(tier => tier.offers.some(offer => offer.model === model));
  return index < 0 ? null : index;
}

export interface AgentPick {
  readonly provider: string;
  readonly model: string;
}

/** The model of `group` that `provider` runs when the reader has not chosen one: its default, else its strongest. */
function fallbackModel(
  offers: readonly HarnessOffer[],
  group: AgentGroup,
  provider: string,
): string | null {
  const defaulted = offersIn(offers, group, provider).find(offer => offer.default);
  if (defaulted !== undefined) return defaulted.model;
  const top = tiersIn(offers, group, provider).at(-1);
  if (top !== undefined) return modelAtTier(top);
  return offersIn(offers, group, provider)[0]?.model ?? null;
}

/**
 * The agent for choosing `group`: the harness already chosen if it runs the line, else the line's
 * first; its model at the same tier as `current` when it has one, else its default.
 */
export function pickGroup(
  offers: readonly HarnessOffer[],
  group: AgentGroup,
  current: AgentPick | null,
): AgentPick | null {
  const provider =
    current !== null && group.providers.includes(current.provider)
      ? current.provider
      : group.providers[0];
  if (provider === undefined) return null;
  return pickHarness(offers, group, provider, current);
}

/** The agent for running `group` on `provider`, keeping the tier of `current` where it exists. */
export function pickHarness(
  offers: readonly HarnessOffer[],
  group: AgentGroup,
  provider: string,
  current: AgentPick | null,
): AgentPick | null {
  const currentOffer =
    current === null
      ? undefined
      : offers.find(each => each.provider === current.provider && each.model === current.model);
  const rank = currentOffer?.display?.tier;
  const sameTier =
    rank === undefined
      ? undefined
      : tiersIn(offers, group, provider).find(tier => tier.rank === rank);
  const model =
    sameTier === undefined ? fallbackModel(offers, group, provider) : modelAtTier(sameTier);
  return model === null ? null : {provider, model};
}
