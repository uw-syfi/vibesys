/**
 * What a stored message says, as typed data: one kind per body the host can send, so the
 * transcript and the spawn tree read a message without looking at its raw `oneof`. The screens
 * turn each kind into words.
 */
import type {AgentStatus, JobOutcome, Message, RunLimit} from '@vibesys/orchestrator-proto';

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
    case 'runLimitReached':
      return {kind: 'limit', limit: body.value.limit};
    case undefined:
      return {kind: 'text', text: ''};
    default:
      body satisfies never;
      return {kind: 'text', text: ''};
  }
}
