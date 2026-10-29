/**
 * The roles a run's agent plays, as the machine's helper reports them (`run.preflight`'s `roles`,
 * feature `run_roles`): the orchestration plugin declares them, so nothing here lists a role. This
 * module holds the rules over that list: what the table shows for a role, and how the form's role
 * settings follow a new list.
 */
import {roleName} from '@vibesys/contracts';
import {type AgentChoice, type AgentPick, defaultRole, type Form, type RoleChoice} from './form.js';

/** A role a person reads ("Judge"): the backend's name for it, else the words of its id. */
export function roleLabel(role: string): string {
  return roleName(role);
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

/** The agent `role` runs: its own, else the run's. */
export function roleAgent(agent: AgentChoice, role: RoleChoice): AgentPick {
  return role.agent ?? {provider: agent.provider, model: agent.model};
}

/**
 * The agent to record for a role that was given `picked` while the run's is `run`: none when they
 * are the same, so a role that was not changed keeps following the run.
 */
export function pickedRoleAgent(run: AgentPick, picked: AgentPick): AgentPick | null {
  return picked.provider === run.provider && picked.model === run.model ? null : picked;
}

/** `roles` with each agent on a harness outside `allowed` put back to the run's. */
export function rolesWithin(
  roles: readonly RoleChoice[],
  allowed: readonly string[],
): RoleChoice[] {
  return roles.map(role =>
    role.agent !== null && !allowed.includes(role.agent.provider) ? {...role, agent: null} : role,
  );
}
