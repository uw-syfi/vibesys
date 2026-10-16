/**
 * The roles a run's agent plays, as the machine's helper reports them (`run.preflight`'s `roles`,
 * feature `run_roles`): the orchestration plugin declares them, so nothing here lists a role. This
 * module holds the rules over that list: what the table shows for a role, and how the form's role
 * settings follow a new list.
 */
import {type AgentChoice, defaultRole, type Form} from './form.js';

/** A role id as a table label: its words, capitalised (`multi-implementer`: `Multi implementer`). */
export function roleLabel(role: string): string {
  const words = role.split(/[-_.]+/).filter(word => word !== '');
  const text = words.join(' ');
  return text === '' ? role : text.charAt(0).toUpperCase() + text.slice(1);
}

/** What the roles named like these usually do, by the last word of their id; copy only. */
const SUMMARIES: readonly (readonly [string, string])[] = [
  ['orchestrator', 'Plans each round'],
  ['profiler', 'Finds hot spots'],
  ['implementer', 'Writes candidates'],
  ['judge', 'Reviews results'],
  ['mutator', 'Writes candidates'],
  ['merger', 'Combines candidates'],
  ['outliner', 'Outlines the work'],
  ['evaluator', 'Measures candidates'],
];

/** One line on what `role` does, or null for a role this copy does not know. */
export function roleSummary(role: string): string | null {
  const found = SUMMARIES.find(([suffix]) => role === suffix || role.endsWith(`-${suffix}`));
  return found === undefined ? null : found[1];
}

/**
 * The roles to show after an answer reported `reported`. An empty report says the settings did not
 * resolve an orchestration, so the roles known so far stay.
 */
export function rolesAfter(
  known: readonly string[] | null,
  reported: readonly string[],
): readonly string[] | null {
  return reported.length === 0 ? known : reported;
}

/** `agent` with exactly the role settings of `roles`: a kept choice, else the default. */
export function agentWithRoles(agent: AgentChoice, roles: readonly string[]): AgentChoice {
  return {
    ...agent,
    roles: roles.map(role => agent.roles.find(each => each.role === role) ?? defaultRole(role)),
  };
}

/** `form` with its agent following `roles`: a choice for a role the run does not have is dropped. */
export function formWithRoles(form: Form, roles: readonly string[] | null): Form {
  if (form.agent === null || roles === null) return form;
  const agent = agentWithRoles(form.agent, roles);
  const same =
    agent.roles.length === form.agent.roles.length &&
    agent.roles.every((role, index) => role === form.agent?.roles[index]);
  return same ? form : {...form, agent};
}
