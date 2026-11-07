/**
 * What a stored message says. `messageIs` is the one place that names every kind of body the host
 * can send (the type makes a kind added to the protocol a compile error here, and nowhere else), so
 * a screen asks `messageIs(message, 'jobFinished')` and never lists the kinds it does not want.
 * `noteOf` reads a message as typed data, one kind per body, for the transcript and the spawn tree;
 * the screens turn each kind into words.
 */
import {
  type AgentStatus,
  type JobOutcome,
  type Message,
  oneofIs,
  type RunLimit,
} from '@vibesys/orchestrator-proto';

type BodyKind = Exclude<Message['body']['case'], undefined>;

/** A message whose body is of kind `K`. */
export type MessageOf<K extends BodyKind> = Message & {
  readonly body: Extract<Message['body'], {readonly case: K}>;
};

/** Every kind of body a message can have. */
const BODY_KINDS = {
  text: true,
  handoff: true,
  report: true,
  jobFinished: true,
  subagentSettled: true,
  brief: true,
  leadBrief: true,
  handoffReminder: true,
  leadIdleReminder: true,
  reportDue: true,
  runLimitReached: true,
} as const satisfies Record<BodyKind, true>;

/** Whether `message` has a body of kind `kind`, and so `message.body.value` is that kind's. */
export function messageIs<K extends BodyKind>(
  message: Message | undefined,
  kind: K,
): message is MessageOf<K> {
  return message !== undefined && oneofIs(message.body, kind, BODY_KINDS);
}

export type MessageNote =
  /** Words someone wrote to an agent. */
  | {readonly kind: 'text'; readonly text: string}
  /** An agent's first prompt: the task of a lead, or what a parent asked of a child. */
  | {readonly kind: 'brief'; readonly text: string}
  /** What a finished agent tells its parent. */
  | {readonly kind: 'handoff'; readonly claim: string}
  | {readonly kind: 'report'; readonly headline: string}
  | {
      readonly kind: 'job';
      readonly jobId: string;
      readonly outcome: JobOutcome;
      readonly detail: string;
    }
  /** A child stopped without handing anything back. */
  | {readonly kind: 'settled'; readonly agentId: string; readonly status: AgentStatus}
  /** The host nudging an agent. */
  | {readonly kind: 'reminder'}
  /** The host asking the lead for a progress report. */
  | {readonly kind: 'report-due'}
  | {readonly kind: 'limit'; readonly limit: RunLimit};

export function noteOf(message: Message): MessageNote {
  const {body} = message;
  switch (body.case) {
    case 'text':
      return {kind: 'text', text: body.value};
    case 'brief':
      return {kind: 'brief', text: body.value};
    case 'leadBrief':
      return {kind: 'brief', text: body.value.task};
    case 'handoff':
      return {kind: 'handoff', claim: body.value.claim};
    case 'report':
      return {kind: 'report', headline: body.value.headline};
    case 'jobFinished':
      return {
        kind: 'job',
        jobId: body.value.jobId,
        outcome: body.value.outcome,
        detail: body.value.detail,
      };
    case 'subagentSettled':
      return {kind: 'settled', agentId: body.value.agentId, status: body.value.status};
    case 'handoffReminder':
    case 'leadIdleReminder':
      return {kind: 'reminder'};
    case 'reportDue':
      return {kind: 'report-due'};
    case 'runLimitReached':
      return {kind: 'limit', limit: body.value.limit};
    case undefined:
      return {kind: 'text', text: ''};
    default:
      body satisfies never;
      return {kind: 'text', text: ''};
  }
}
