/**
 * The names the demos give the roles they draw, taken from the catalog like any screen so a
 * rename shows here too and no demo spells a role word itself.
 */
import {roleLabel} from '@vibesys/feature-new-run/state';

/** What a screen calls the role that plans each round. */
export const PLANNER = roleLabel('orchestrator');
/** What a screen calls the role that writes candidates. */
const BUILDER = roleLabel('implementer');
/** What a screen calls the role that reviews them. */
export const REVIEWER = roleLabel('judge');
/** Lowercase, for the demos that label a bar or a swatch the way a legend does. */
export const PLANNER_LOWER = PLANNER.toLowerCase();
export const BUILDER_LOWER = BUILDER.toLowerCase();
export const REVIEWER_LOWER = REVIEWER.toLowerCase();
