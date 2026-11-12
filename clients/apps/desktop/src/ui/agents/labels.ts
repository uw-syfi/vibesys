/**
 * The words a person reads for an agent: its role as the catalog names it, capitalised, and its number
 * (`Planner 2`), as the tree, the transcript, the report and every other screen say it.
 */
import type {AgentRole} from '@vibesys/orchestrator-proto';
import type {AgentName, AgentRuntime} from '../../state/index.js';
import {harnessWord, roleWord} from '../orchestrator/words.js';

const roleLabel = (role: AgentRole): string => roleWord(role);

/** `Lead`, `Planner 2`: the role, and the number among the agents of that role unless it is the lead's. */
export const agentLabel = (name: AgentName): string =>
  name.number === null ? roleLabel(name.role) : `${roleLabel(name.role)} ${name.number}`;

/** A group of agents of one role, as its row says it. */
export const groupLabel = roleLabel;

/** The runtime beside an agent: a named model on its tool, or that tool's own default. */
export const runtimeLabel = (runtime: AgentRuntime): string =>
  runtime.model === ''
    ? `${harnessWord(runtime.harness)} default`
    : `${runtime.model} on ${harnessWord(runtime.harness)}`;
