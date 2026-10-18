/**
 * How the Run screen names what ran: a role, a model, a step. The ids come from the run's events;
 * the names a person reads come from the backend registries (`@vibesys/contracts`'
 * `roleName`), so no screen draws an id such as `perf-evaluator`.
 */
import {roleName} from '@vibesys/contracts';
import type {CandidateStep} from '@vibesys/run-state/candidates';
import {plain} from './span-words.js';

/**
 * A step's title for a person. A search titles an agent turn by its role's id (`judge`), which
 * reads as the role's name; any other title (an idea's, a gate's) is already words.
 */
export function stepTitle(step: Pick<CandidateStep, 'title' | 'role'>): string {
  return step.role !== null && step.title === step.role ? roleName(step.role) : step.title;
}

/** What a step is called in a list or a crumb: its role's name, else its title. */
export function stepLabel(step: Pick<CandidateStep, 'title' | 'role'>): string {
  return step.role === null ? step.title : roleName(step.role);
}

/** An agent role a person reads ("Judge", "Perf evaluator"): the backend's name for it. */
export function roleLabel(role: string): string {
  return roleName(role);
}

/**
 * The kicker over a step's pane title: what kind of step it is, and its role unless the title
 * already names it (an agent turn titled by its role reads `Agent turn` over `Implementer`, not
 * `Agent turn · Implementer` over `Implementer`).
 */
export function stepKicker(step: Pick<CandidateStep, 'kind' | 'title' | 'role'>): string {
  const kind = plain(step.kind);
  if (step.role === null) return kind;
  return stepTitle(step) === roleName(step.role) ? kind : `${kind} · ${roleName(step.role)}`;
}
