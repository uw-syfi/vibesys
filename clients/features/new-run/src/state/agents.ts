/**
 * The agent the host offers, as the Agent question reads it: model lines (cards), the harnesses
 * that run one, and the models of a line from smaller and cheaper to stronger (a slider). It reads
 * only what each offer's `display` says today (family, tier); a model line the host does not rank
 * is a flat list. This module is the one place that knows which display fields exist, so a host
 * that also reports makers and generations changes it and nothing else.
 */
import type {HarnessOffer} from '@vibesys/ipc';
import {type AgentEnvironment, type AgentPick, harnessLabel, modelLabel} from './form.js';
import {pickedRoleAgent} from './roles.js';

/** A maker of the agent's own models, as the host reports it. */
export type AgentMaker = NonNullable<HarnessOffer['display']>['maker'] & string;

/** The makers the host reports, as people name them. */
const MAKER_NAMES: Readonly<Record<AgentMaker, string>> = {
  anthropic: 'Anthropic',
  openai: 'OpenAI',
  google: 'Google',
};

/** One card of the Agent question: a maker's models (or a family, or a harness, when the host says no more) and the harnesses that run them. */
export interface AgentGroup {
  readonly id: string;
  readonly name: string;
  /** The maker, when the host names one. */
  readonly maker: AgentMaker | null;
  /** The harnesses with at least one model of the line, in the host's order. */
  readonly providers: readonly string[];
}

export interface ModelTier {
  readonly rank: number;
  readonly name: string;
  readonly offers: readonly HarnessOffer[];
}

/** The card an offer belongs to: its maker when the host names one, else its family, else its harness alone. */
function groupIdOf(offer: HarnessOffer): string {
  const {maker, family} = offer.display ?? {};
  if (maker !== undefined) return `maker:${maker}`;
  return family === undefined ? `harness:${offer.provider}` : `family:${family}`;
}

/** The model lines `offers` make, in the order the host first lists them. */
export function agentGroups(offers: readonly HarnessOffer[]): AgentGroup[] {
  const groups = new Map<string, {maker: AgentMaker | null; name: string; providers: string[]}>();
  for (const offer of offers) {
    const id = groupIdOf(offer);
    const group = groups.get(id) ?? {
      maker: offer.display?.maker ?? null,
      name:
        offer.display?.maker === undefined
          ? (offer.display?.family ?? harnessLabel(offers, offer.provider))
          : MAKER_NAMES[offer.display.maker],
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

/** Which of a card's models: a family and a generation of it, each null when the host names none. */
export interface Scope {
  readonly family: string | null;
  readonly generation: string | null;
}

/** The scope `offer` sits in. */
export function scopeOf(offer: HarnessOffer): Scope {
  return {family: offer.display?.family ?? null, generation: offer.display?.generation?.id ?? null};
}

function inScope(offer: HarnessOffer, scope: Scope | null): boolean {
  if (scope === null) return true;
  const own = scopeOf(offer);
  return own.family === scope.family && own.generation === scope.generation;
}

/** The offers of `group` that `provider` runs (those in `scope`, when given), in the host's order. */
export function offersIn(
  offers: readonly HarnessOffer[],
  group: AgentGroup,
  provider: string,
  scope: Scope | null = null,
): HarnessOffer[] {
  return offers.filter(
    offer => offer.provider === provider && groupIdOf(offer) === group.id && inScope(offer, scope),
  );
}

/** The families a card's models on `provider` come in, in the host's order; empty with fewer than two. */
export function familiesIn(
  offers: readonly HarnessOffer[],
  group: AgentGroup,
  provider: string,
): string[] {
  const families = offersIn(offers, group, provider).flatMap(offer => {
    const family = offer.display?.family;
    return family === undefined ? [] : [family];
  });
  const distinct = [...new Set(families)];
  return distinct.length > 1 ? distinct : [];
}

export interface Generation {
  readonly id: string;
  readonly name: string;
}

/**
 * The generations of `family` on `provider`, newest first (the host's `order` ascends from the
 * newest); empty when the host names none or only one, so the generation row appears only when
 * there is something to choose.
 */
export function generationsIn(
  offers: readonly HarnessOffer[],
  group: AgentGroup,
  provider: string,
  family: string | null,
): Generation[] {
  const found = new Map<string, {name: string; order: number}>();
  for (const offer of offersIn(offers, group, provider)) {
    const generation = offer.display?.generation;
    if (generation === undefined || (offer.display?.family ?? null) !== family) continue;
    found.set(generation.id, {name: generation.name, order: generation.order});
  }
  const sorted = [...found].sort((left, right) => left[1].order - right[1].order);
  return sorted.length > 1 ? sorted.map(([id, each]) => ({id, name: each.name})) : [];
}

/**
 * The tiers of `group` on `provider`, from smallest to strongest; empty when the host does not
 * rank them or there is nothing to choose between.
 */
export function tiersIn(
  offers: readonly HarnessOffer[],
  group: AgentGroup,
  provider: string,
  scope: Scope | null = null,
): ModelTier[] {
  const tiers = new Map<number, ModelTier>();
  for (const offer of offersIn(offers, group, provider, scope)) {
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

/** The model of `group` that `provider` runs when the reader has not chosen one: its default, else its strongest. */
function fallbackModel(
  offers: readonly HarnessOffer[],
  group: AgentGroup,
  provider: string,
  scope: Scope | null,
): string | null {
  const own = offersIn(offers, group, provider, scope);
  const defaulted = own.find(offer => offer.default);
  if (defaulted !== undefined) return defaulted.model;
  const top = tiersIn(offers, group, provider, scope).at(-1);
  if (top !== undefined) return modelAtTier(top);
  return own[0]?.model ?? null;
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

/**
 * The agent for running `group` on `provider` (within `scope`, when given), keeping the tier of
 * `current` where it exists.
 */
export function pickHarness(
  offers: readonly HarnessOffer[],
  group: AgentGroup,
  provider: string,
  current: AgentPick | null,
  scope: Scope | null = null,
): AgentPick | null {
  const currentOffer =
    current === null
      ? undefined
      : offers.find(each => each.provider === current.provider && each.model === current.model);
  const rank = currentOffer?.display?.tier;
  const sameTier =
    rank === undefined
      ? undefined
      : tiersIn(offers, group, provider, scope).find(tier => tier.rank === rank);
  const model =
    sameTier === undefined ? fallbackModel(offers, group, provider, scope) : modelAtTier(sameTier);
  return model === null ? null : {provider, model};
}

/**
 * The harnesses a role may use when the run's is `runProvider`: where agents run in containers the
 * helper gives a container the login of the run's provider only, so it is that one alone; on the
 * host, any the machine offers.
 */
export function roleProviders(
  offers: readonly HarnessOffer[],
  runProvider: string,
  environment: AgentEnvironment,
): string[] {
  return environment === 'container' ? [runProvider] : [...new Set(offers.map(o => o.provider))];
}

/** One model a role may use, with the harness that runs it. */
export interface RoleModel {
  /** Unique among a list's options. */
  readonly key: string;
  readonly provider: string;
  readonly model: string;
  readonly label: string;
  /** "Anthropic · Claude 5": the maker, then the generation (else the family) the model is in. */
  readonly group: string;
}

/**
 * The models a role may choose among, one option per model, grouped by maker and then generation
 * (else family), the groups and the models in the host's order. A model several allowed harnesses
 * run is listed once, on `prefer`'s harness when it runs it. When following a run agent, its
 * selection already has an inheritance option, so the equivalent model is not listed twice.
 */
export function roleModels(
  offers: readonly HarnessOffer[],
  providers: readonly string[],
  prefer: string,
  following: AgentPick | null = null,
): RoleModel[] {
  const names = new Map(agentGroups(offers).map(group => [group.id, group.name]));
  const chosen = new Map<string, HarnessOffer>();
  for (const offer of offers) {
    if (!providers.includes(offer.provider)) continue;
    const before = chosen.get(offer.model);
    if (before === undefined || (offer.provider === prefer && before.provider !== prefer)) {
      chosen.set(offer.model, offer);
    }
  }
  const groups = new Map<string, RoleModel[]>();
  for (const offer of chosen.values()) {
    const line = names.get(groupIdOf(offer)) ?? harnessLabel(offers, offer.provider);
    const within = offer.display?.generation?.name ?? offer.display?.family ?? null;
    const group = within === null || within === line ? line : `${line} \u00b7 ${within}`;
    const options = groups.get(group) ?? [];
    options.push({
      key: JSON.stringify([offer.provider, offer.model]),
      provider: offer.provider,
      model: offer.model,
      label: modelLabel(offers, offer.provider, offer.model),
      group,
    });
    groups.set(group, options);
  }
  return [...groups.values()]
    .flat()
    .filter(model => following === null || pickedRoleAgent(following, model) !== null);
}

/** The allowed harnesses that run `model`, in the host's order. */
export function roleHarnesses(
  offers: readonly HarnessOffer[],
  providers: readonly string[],
  model: string,
): string[] {
  return [
    ...new Set(
      offers
        .filter(offer => offer.model === model && providers.includes(offer.provider))
        .map(offer => offer.provider),
    ),
  ];
}
