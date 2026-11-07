/**
 * What the screens say. Every state, role and outcome is named here in plain words, so a screen
 * never shows a protocol name and a new value cannot reach the user unnamed.
 */
import {roleName} from '@vibesys/display-names';
import {
  AgentRole,
  AgentStatus,
  Delivery,
  Harness,
  JobOutcome,
  Refusal,
  Rejection,
  RunStatus,
  type ToolCall,
} from '@vibesys/orchestrator-proto';
import type {FaultKind} from '../../state/index.js';

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

/** Where a message stands, in the contract's own terms: a steered message is sent, not yet confirmed. */
const DELIVERY: Readonly<Record<Delivery, Word>> = {
  [Delivery.UNSPECIFIED]: {label: 'Not sent', tone: 'neutral'},
  [Delivery.QUEUED]: {label: 'Accepted, waiting for the agent\u2019s next turn', tone: 'neutral'},
  [Delivery.STEERED]: {label: 'Sent; delivery to agent unconfirmed', tone: 'live'},
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
  [Refusal.UNSPECIFIED]: 'The host did not accept that.',
  [Refusal.RUN_NOT_FOUND]: 'That run no longer exists.',
  [Refusal.AGENT_NOT_FOUND]: 'No such agent in this run.',
  [Refusal.NOT_PERMITTED]: 'This app is not allowed to do that.',
  [Refusal.RUN_NOT_RUNNING]: 'The run is not running.',
  [Refusal.FANOUT_LIMIT]: 'The run is already at its limit of agents.',
  [Refusal.AGENT_IS_LEAD]: 'That message cannot go to the lead.',
  [Refusal.AGENT_NOT_LIVE]: 'That agent has finished and cannot take messages.',
  [Refusal.REPOSITORY_IN_USE]: 'Another run is already working in this repository.',
  [Refusal.AGENT_NOT_ALLOWED]: 'The run was not allowed to use that agent.',
  [Refusal.HANDOFF_NOT_YOURS]: 'That handoff does not belong to the agent\u2019s role.',
  [Refusal.TIME_LIMIT_REACHED]: 'The run reached its time limit and starts no new agent.',
  [Refusal.COST_LIMIT_REACHED]: 'The run reached its cost limit and starts no new agent.',
  [Refusal.NO_VIEWER]: 'This app did not say who is looking.',
};

export const refusalWord = (refusal: Refusal): string => REFUSAL[refusal];

export function formatDuration(totalMs: number): string {
  const seconds = Math.max(0, Math.round(totalMs / 1000));
  if (seconds < 60) return `${seconds} s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} min ${seconds % 60} s`;
  return `${Math.floor(minutes / 60)} h ${minutes % 60} min`;
}

export function formatClock(atMs: number): string {
  return new Date(atMs).toISOString().slice(11, 16);
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

/** A moment with its date and the zone it is told in, for lists that span days: `Nov 1, 17:00 UTC`. */
export function formatMoment(atMs: number): string {
  const at = new Date(atMs);
  return `${MONTHS[at.getUTCMonth()]} ${at.getUTCDate()}, ${formatClock(atMs)} UTC`;
}

/** Why a connection or a request did not work, in a sentence a person can act on. */
export function faultWord(kind: FaultKind): string {
  switch (kind) {
    case 'AuthenticationFailed':
      return 'SSH key authentication failed. Configure a key accepted by the host and try again.';
    case 'NoBridge':
      return 'This page is not running inside the VibeSys app, so it has no connection to a host.';
    case 'TransportFailed':
      return 'The connection to the host was lost.';
    case 'Unavailable':
      return 'The host cannot be reached right now.';
    case 'DeadlineExceeded':
      return 'The host did not answer in time.';
    case 'FellBehind':
      return 'The app could not keep up with the host.';
    case 'InvalidMessage':
      return 'The host sent something this app does not understand.';
    case 'NotFound':
      return 'The host no longer has what was asked for.';
    case 'Unimplemented':
      return 'The VibeSys on that host is too old for this app. Update it there.';
    case 'Rejected':
      return 'The host refused the request.';
  }
}

/** What a person is told of one tool call: its name, and what it was asked as text. */
export interface CallWords {
  readonly name: string;
  readonly asked: string | null;
}

/** A tool the host did not name, by the words of its own name. */
function otherName(name: string): string {
  const words = name
    .replace(/^mcp__.+?__/, '')
    .replace(/[_-]+/g, ' ')
    .trim();
  return words === '' ? 'Tool' : words.charAt(0).toUpperCase() + words.slice(1);
}

function orNull(text: string): string | null {
  return text === '' ? null : text;
}

/**
 * A tool call as the words a person reads, from the typed call the host sent. Never the JSON the
 * tool received and never a provider's argument names: the host has already said what the call is.
 */
export function describeCall(call: ToolCall): CallWords {
  const use = call.call;
  switch (use.case) {
    case 'shell':
      return {name: 'Command', asked: orNull(use.value.command)};
    case 'read':
      return {name: 'Read a file', asked: orNull(use.value.target)};
    case 'write':
      return {name: 'Wrote a file', asked: orNull(use.value.target)};
    case 'edit':
      return {
        name: use.value.targets.length > 1 ? 'Edited files' : 'Edited a file',
        asked: orNull(use.value.targets.join('\n')),
      };
    case 'search':
      return {name: 'Searched the files', asked: orNull(use.value.pattern)};
    case 'other':
      return {name: otherName(use.value.name), asked: orNull(use.value.text)};
    case undefined:
      return {name: 'Tool', asked: null};
    default:
      use satisfies never;
      return {name: 'Tool', asked: null};
  }
}
