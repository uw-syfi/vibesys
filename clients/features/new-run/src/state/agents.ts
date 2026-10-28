/**
 * The agent the host offers, as Review's Agent rows read it: the models, grouped by model line
 * (maker, else family, else harness) and then generation, and the harnesses that run each. It reads
 * only what each offer's `display` says; this module is the one place that knows which display
 * fields exist, so a host that reports more changes it and nothing else.
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

/** One model line: a maker's models (or a family, or a harness, when the host says no more) and the harnesses that run them. */
export interface AgentGroup {
  readonly id: string;
  readonly name: string;
  /** The maker, when the host names one. */
  readonly maker: AgentMaker | null;
  /** The harnesses with at least one model of the line, in the host's order. */
  readonly providers: readonly string[];
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
