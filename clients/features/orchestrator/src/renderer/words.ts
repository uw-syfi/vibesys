/**
 * What the screens say. Every state, role and outcome is named here in plain words, so a screen
 * never shows a protocol name and a new value cannot reach the user unnamed.
 */
import {roleName} from '@vibesys/contracts';
import {
  AgentRole,
  AgentStatus,
  Delivery,
  Harness,
  JobOutcome,
  PlanStatus,
  Refusal,
  Rejection,
  RunStatus,
  TurnStatus,
} from '@vibesys/orchestrator-proto';
import type {Fault} from '../state/index.js';

type Tone = 'neutral' | 'live' | 'ok' | 'warn' | 'err';

export interface Word {
  readonly label: string;
  readonly tone: Tone;
}

/** The catalog ids of the six roles; the catalog turns each into the words a person reads. */
const ROLE_ID: Readonly<Record<AgentRole, string>> = {
  [AgentRole.UNSPECIFIED]: 'helper',
  [AgentRole.LEAD]: 'lead',
  [AgentRole.PLANNER]: 'planner',
  [AgentRole.WORKER]: 'worker',
  [AgentRole.JUDGE]: 'judge',
  [AgentRole.COMBINER]: 'combiner',
  [AgentRole.PROFILER]: 'profiler',
};

export const roleWord = (role: AgentRole): string => roleName(ROLE_ID[role]);

const AGENT: Readonly<Record<AgentStatus, Word>> = {
  [AgentStatus.UNSPECIFIED]: {label: 'Unknown', tone: 'neutral'},
  [AgentStatus.LAUNCHING]: {label: 'Starting', tone: 'live'},
  [AgentStatus.ACTIVE]: {label: 'Working', tone: 'live'},
  [AgentStatus.WAITING]: {label: 'Waiting', tone: 'neutral'},
  [AgentStatus.IDLE]: {label: 'Done for now', tone: 'ok'},
  [AgentStatus.ENDED]: {label: 'Finished', tone: 'ok'},
  [AgentStatus.FAILED]: {label: 'Failed', tone: 'err'},
  [AgentStatus.LOST]: {label: 'Lost contact', tone: 'warn'},
};

export const agentWord = (status: AgentStatus): Word => AGENT[status];

const RUN: Readonly<Record<RunStatus, Word>> = {
  [RunStatus.UNSPECIFIED]: {label: 'Unknown', tone: 'neutral'},
  [RunStatus.STARTING]: {label: 'Starting', tone: 'live'},
  [RunStatus.RUNNING]: {label: 'Running', tone: 'live'},
  [RunStatus.STOPPING]: {label: 'Stopping', tone: 'warn'},
  [RunStatus.ENDED]: {label: 'Finished', tone: 'ok'},
  [RunStatus.FAILED]: {label: 'Failed', tone: 'err'},
};

export const runWord = (status: RunStatus): Word => RUN[status];

const TURN: Readonly<Record<TurnStatus, string>> = {
  [TurnStatus.UNSPECIFIED]: 'Unknown',
  [TurnStatus.RUNNING]: 'In progress',
  [TurnStatus.COMPLETED]: 'Completed',
  [TurnStatus.INTERRUPTED]: 'Interrupted',
  [TurnStatus.FAILED]: 'Failed',
};

export const turnWord = (status: TurnStatus): string => TURN[status];

const HARNESS: Readonly<Record<Harness, string>> = {
  [Harness.UNSPECIFIED]: 'Unknown tool',
  [Harness.CLAUDE_CODE]: 'Claude Code',
  [Harness.CODEX]: 'Codex',
};

export const harnessWord = (harness: Harness): string => HARNESS[harness];

const JOB: Readonly<Record<JobOutcome, Word>> = {
  [JobOutcome.UNSPECIFIED]: {label: 'Unknown', tone: 'neutral'},
  [JobOutcome.SUCCEEDED]: {label: 'Succeeded', tone: 'ok'},
  [JobOutcome.FAILED]: {label: 'Failed', tone: 'err'},
  [JobOutcome.CANCELLED]: {label: 'Cancelled', tone: 'warn'},
  [JobOutcome.LOST]: {label: 'Lost', tone: 'warn'},
};

export const jobWord = (outcome: JobOutcome): Word => JOB[outcome];

const PLAN: Readonly<Record<PlanStatus, Word>> = {
  [PlanStatus.UNSPECIFIED]: {label: 'Unknown', tone: 'neutral'},
  [PlanStatus.OPEN]: {label: 'Not tried yet', tone: 'neutral'},
  [PlanStatus.CHOSEN]: {label: 'Being tried', tone: 'live'},
  [PlanStatus.WON]: {label: 'Worked', tone: 'ok'},
  [PlanStatus.PARKED]: {label: 'Set aside', tone: 'warn'},
  [PlanStatus.REJECTED]: {label: 'Did not work', tone: 'err'},
};

export const planWord = (status: PlanStatus): Word => PLAN[status];

/** The order ideas are listed in, most useful first. */
export const PLAN_ORDER: readonly PlanStatus[] = [
  PlanStatus.CHOSEN,
  PlanStatus.OPEN,
  PlanStatus.WON,
  PlanStatus.PARKED,
  PlanStatus.REJECTED,
];

/** Where a message stands, in the contract's own terms: a steered message is sent, not yet confirmed. */
const DELIVERY: Readonly<Record<Delivery, Word>> = {
  [Delivery.UNSPECIFIED]: {label: 'Not sent', tone: 'neutral'},
  [Delivery.QUEUED]: {label: 'Accepted, waiting for the agent\u2019s next turn', tone: 'neutral'},
  [Delivery.STEERED]: {label: 'Sent, not yet confirmed', tone: 'live'},
  [Delivery.NEW_TURN]: {label: 'Being handled in the agent\u2019s current turn', tone: 'live'},
  [Delivery.CONSUMED]: {label: 'Read by the agent', tone: 'ok'},
  [Delivery.REJECTED]: {label: 'Not delivered', tone: 'err'},
};

export const deliveryWord = (delivery: Delivery): Word => DELIVERY[delivery];

const REJECTION: Readonly<Record<Rejection, string>> = {
  [Rejection.UNSPECIFIED]: 'No reason was given.',
  [Rejection.AGENT_GONE]: 'The agent\u2019s session is over and cannot be resumed.',
  [Rejection.FANOUT_LIMIT]: 'Waking the agent would go over the run\u2019s limit of agents.',
  [Rejection.AGENT_STOPPING]: 'The agent is being stopped.',
  [Rejection.RUN_STOPPING]: 'The run is stopping or over.',
  [Rejection.ATTEMPTS_EXHAUSTED]:
    'Every attempt to deliver it ended in an interrupted or failed turn.',
};

export const rejectionWord = (rejection: Rejection): string => REJECTION[rejection];

const REFUSAL: Readonly<Record<Refusal, string>> = {
  [Refusal.UNSPECIFIED]: 'The message was not accepted.',
  [Refusal.RUN_NOT_FOUND]: 'That run no longer exists.',
  [Refusal.AGENT_NOT_FOUND]: 'No such agent in this run.',
  [Refusal.NOT_PERMITTED]: 'You are not allowed to message that agent.',
  [Refusal.RUN_NOT_RUNNING]: 'The run is not running.',
  [Refusal.FANOUT_LIMIT]: 'The run is already at its limit of agents.',
  [Refusal.AGENT_IS_LEAD]: 'That message cannot go to the lead.',
  [Refusal.AGENT_NOT_LIVE]: 'That agent has finished and cannot take messages.',
  [Refusal.DEADLINE_PASSED]: 'The run passed its deadline.',
};

export const refusalWord = (refusal: Refusal): string => REFUSAL[refusal];

export function formatDuration(totalMs: number): string {
  const seconds = Math.max(0, Math.round(totalMs / 1000));
  if (seconds < 60) return `${seconds} s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} min ${seconds % 60} s`;
  return `${Math.floor(minutes / 60)} h ${minutes % 60} min`;
}

/** A count of tokens in thousands (`12.3k`), or `Not reported` where the tool said nothing. */
export function formatTokens(count: number | null): string {
  if (count === null) return 'Not reported';
  return count < 1000 ? String(count) : `${(count / 1000).toFixed(1)}k`;
}

export const formatCost = (usd: number | null): string =>
  usd === null ? 'Not reported' : `$${usd.toFixed(2)}`;

export function formatClock(atMs: number): string {
  return new Date(atMs).toISOString().slice(11, 16);
}

/** Why a connection or a request did not work, in a sentence a person can act on. */
export function faultWord(kind: Fault['kind']): string {
  switch (kind) {
    case 'lost':
      return 'The connection to the host was lost.';
    case 'refused':
      return 'The host refused the request.';
    case 'invalid':
      return 'The host sent something this app does not understand.';
    case 'ended':
      return 'The host closed the connection.';
    case 'timeout':
      return 'The host did not answer in time.';
  }
}

/** A tool as a person names it, for the tools whose internal name is not a word. */
const TOOL: Readonly<Record<string, string>> = {
  shell: 'Command',
  bash: 'Command',
  exec: 'Command',
  exec_command: 'Command',
  read_file: 'Read a file',
  write_file: 'Wrote a file',
  apply_patch: 'Edited files',
  edit: 'Edited a file',
  grep: 'Searched the files',
  glob: 'Looked for files',
};

export function toolLabel(tool: string): string {
  const known = TOOL[tool];
  if (known !== undefined) return known;
  const words = tool.replace(/[_-]+/g, ' ').trim();
  return words === '' ? 'Tool' : words.charAt(0).toUpperCase() + words.slice(1);
}

/**
 * What a tool call was asked to do, as text a person reads: the command when the arguments have
 * one, otherwise each argument on its own line. Never the JSON the tool received.
 */
export function describeArgs(argsJson: string): string | null {
  if (argsJson === '') return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(argsJson);
  } catch {
    return argsJson;
  }
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) return String(parsed);
  const args = parsed as Record<string, unknown>;
  const command = args['command'] ?? args['cmd'];
  if (typeof command === 'string') return command;
  if (Array.isArray(command)) return command.map(String).join(' ');
  return Object.entries(args)
    .map(([key, value]) => `${key}: ${typeof value === 'string' ? value : JSON.stringify(value)}`)
    .join('\n');
}
