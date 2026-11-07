/** Which models the Start run form offers for a lead tool, from what the host says it runs and allows. */
import type {Harness, ModelCatalog} from '@vibesys/orchestrator-proto';

export interface ModelChoices {
  /**
   * The models to offer for the tool, the one to start on first. On a host with no policy these are
   * the tool's own models, and the tool's default is a choice of its own, no model at all.
   */
  readonly models: readonly string[];
  /** The host allows only some models, so the form always names one and never leaves it to the tool. */
  readonly pinned: boolean;
  /** Why no model can be chosen for the tool, or null when one can. */
  readonly blocked: string | null;
}

/** What the form offers before the host has said anything: the tool's own default. */
export const NO_CATALOG: ModelChoices = {models: [], pinned: false, blocked: null};

/**
 * The choices for `harness`. Where the host lists the models it allows, they are those, in its order,
 * that this tool runs or that no other tool is known to run (a model the tool's own list lacks may be
 * a newer one); where it lists none, the tool's own models. A host whose policy cannot be read allows
 * nothing, and a tool with none of the allowed models cannot be started.
 */
export function modelChoices(catalog: ModelCatalog | null, harness: Harness): ModelChoices {
  if (catalog === null) return NO_CATALOG;
  if (catalog.unreadablePolicy) {
    return {models: [], pinned: true, blocked: 'The host’s settings cannot be read.'};
  }
  const known = catalog.harnesses.find(entry => entry.harness === harness)?.models ?? [];
  if (catalog.allowedModels.length === 0) return {models: known, pinned: false, blocked: null};
  const others = new Set(
    catalog.harnesses.filter(entry => entry.harness !== harness).flatMap(entry => entry.models),
  );
  const models = catalog.allowedModels.filter(model => known.includes(model) || !others.has(model));
  return {
    models,
    pinned: true,
    blocked: models.length === 0 ? 'No allowed model runs on this tool.' : null,
  };
}

/**
 * The model a start names: the one chosen when the choices offer it, else the first of a host that
 * allows only some, else none (the tool's default). The empty string is none, as on the wire.
 */
export function startModel(chosen: string, choices: ModelChoices): string {
  if (choices.models.includes(chosen)) return chosen;
  return choices.pinned ? (choices.models[0] ?? '') : '';
}
