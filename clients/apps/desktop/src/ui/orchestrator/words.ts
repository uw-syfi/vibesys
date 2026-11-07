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
import type {Fault, FaultKind} from '../../state/index.js';
import {causeHint, causeWord} from '../refusal-words.js';

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
  [Delivery.QUEUED]: {label: 'Queued', tone: 'neutral'},
  [Delivery.STEERED]: {label: 'Sent, unconfirmed', tone: 'live'},
  [Delivery.NEW_TURN]: {label: 'In its turn', tone: 'live'},
  [Delivery.CONSUMED]: {label: 'Read', tone: 'ok'},
  [Delivery.REJECTED]: {label: 'Not delivered', tone: 'err'},
};

export const deliveryWord = (delivery: Delivery): Word => DELIVERY[delivery];

/** Why a message was not delivered, as a label. */
const REJECTION: Readonly<Record<Rejection, string>> = {
  [Rejection.UNSPECIFIED]: 'No reason given',
  [Rejection.AGENT_GONE]: 'Session over',
  [Rejection.FANOUT_LIMIT]: 'At agent limit',
  [Rejection.AGENT_STOPPING]: 'Agent stopping',
  [Rejection.RUN_STOPPING]: 'Run stopping',
  [Rejection.ATTEMPTS_EXHAUSTED]: 'Attempts failed',
};

export const rejectionWord = (rejection: Rejection): string => REJECTION[rejection];

/** Why the host refused a command, as a label. */
const REFUSAL: Readonly<Record<Refusal, string>> = {
  [Refusal.UNSPECIFIED]: 'Not accepted',
  [Refusal.RUN_NOT_FOUND]: 'Run gone',
  [Refusal.AGENT_NOT_FOUND]: 'No such agent',
  [Refusal.NOT_PERMITTED]: 'Not permitted',
  [Refusal.RUN_NOT_RUNNING]: 'Not running',
  [Refusal.FANOUT_LIMIT]: 'At agent limit',
  [Refusal.AGENT_IS_LEAD]: 'Not for the lead',
  [Refusal.AGENT_NOT_LIVE]: 'Agent finished',
  [Refusal.REPOSITORY_IN_USE]: 'Repository in use',
  [Refusal.AGENT_NOT_ALLOWED]: 'Agent not allowed',
  [Refusal.HANDOFF_NOT_YOURS]: 'Not this role\u2019s handoff',
  [Refusal.TIME_LIMIT_REACHED]: 'Time limit reached',
  [Refusal.COST_LIMIT_REACHED]: 'Cost limit reached',
  [Refusal.NO_VIEWER]: 'No viewer named',
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

/** Why a connection or a request did not work, as a label. */
export function faultWord(kind: FaultKind): string {
  switch (kind) {
    case 'AuthenticationFailed':
      return 'Key refused';
    case 'NoBridge':
      return 'No host link';
    case 'TransportFailed':
      return 'Connection lost';
    case 'Unavailable':
      return 'Unreachable';
    case 'DeadlineExceeded':
      return 'Timed out';
    case 'FellBehind':
      return 'Fell behind';
    case 'InvalidMessage':
      return 'Invalid reply';
    case 'NotFound':
      return 'Not on host';
    case 'Unimplemented':
      return 'Host too old';
    case 'Rejected':
      return 'Rejected';
    case 'Refused':
      return 'Refused';
  }
}

/** What to do about a fault, for a hover: the sentence the label stands for. */
export function faultAdvice(kind: FaultKind): string {
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
    case 'Refused':
      return 'The request was refused.';
  }
}

/** A fault as a label: its cause when it is a typed refusal, else the kind's. */
export const faultText = (fault: Fault): string =>
  fault.cause === undefined ? faultWord(fault.kind) : causeWord(fault.cause);

/** The account behind a fault's label, for a hover: the cause's when typed, else the kind's. */
export const faultHint = (fault: Fault): string =>
  fault.cause === undefined ? faultAdvice(fault.kind) : causeHint(fault.cause);

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
