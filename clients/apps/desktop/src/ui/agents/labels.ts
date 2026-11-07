/** The words a person reads for an agent: its role and number (`planner-2`), as the tree and the transcript say it. */
import type {AgentRole} from '@vibesys/orchestrator-proto';
import type {AgentName} from '../../state/index.js';
import {roleWord} from '../orchestrator/words.js';

const roleLabel = (role: AgentRole): string => roleWord(role).toLowerCase();

/** `lead`, `planner-2`: the role, and the number among the agents of that role unless it is the lead's. */
export const agentLabel = (name: AgentName): string =>
  name.number === null ? roleLabel(name.role) : `${roleLabel(name.role)}-${name.number}`;

/** A group of agents of one role, as its row says it. */
export const groupLabel = roleLabel;
