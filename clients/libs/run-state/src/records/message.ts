/** Presentation of recorded values only; lifecycle and measurement authority stay in their folds. */
import {type ChatRecord, type Lifecycle, Outcome, StageState, StartupStage} from '@vibesys/proto';
import type {RowBody} from './kinds.js';

function chatMessage(chat: ChatRecord): string {
  const event = chat.event;
  switch (event.case) {
    case 'created':
      return `Chat: ${event.value.title}`;
    case 'message':
      return event.value.content.case === 'text'
        ? event.value.content.value
        : 'Chat message attachment';
    case 'token':
    case 'proposedSteer':
      return event.value.text;
    case undefined:
      throw new Error('chat.event required');
  }
}

function lifecycleMessage(lifecycle: Lifecycle): string {
  const state = lifecycle.state;
  switch (state.case) {
    case 'starting':
      return `Starting: ${StartupStage[state.value.stage].toLowerCase().replaceAll('_', ' ')} (${StageState[state.value.state].toLowerCase()})`;
    case 'running':
      return state.value.hold ? 'Run on hold' : 'Run running';
    case 'draining':
      return 'Run draining';
    case 'parked':
      return 'Run paused';
    case 'ended':
      return [
        `Run ${Outcome[state.value.outcome].toLowerCase()}`,
        state.value.failure?.reason || state.value.summary || state.value.interruptionReason,
      ]
        .filter(Boolean)
        .join(': ');
    case undefined:
      throw new Error('lifecycle.state required');
  }
}

export function recordMessage(body: RowBody): string {
  switch (body.case) {
    case 'epochStarted':
      return `Epoch ${body.value.epoch} started`;
    case 'declarationAmended':
      return `Epoch ${body.value.epoch} declaration updated`;
    case 'legacyEvent':
      return body.value.json;
    case 'lifecycle':
      return lifecycleMessage(body.value);
    case 'span':
      return (
        [
          body.value.span?.label?.title || body.value.span?.name,
          body.value.span?.label?.reason || body.value.span?.failure?.message,
        ]
          .filter(Boolean)
          .join(': ') || 'Span updated'
      );
    case 'metric':
      return `${body.value.series}: ${body.value.value}`;
    case 'stream':
      return `Stream: ${body.value.title || body.value.id}`;
    case 'blob':
      return `${body.value.role}: ${body.value.blob?.sha256 ?? ''}`;
    case 'intent':
      return `Control ${body.value.controlId}${body.value.reason ? `: ${body.value.reason}` : ''}`;
    case 'notice':
      return body.value.message;
    case 'chat':
      return chatMessage(body.value);
  }
}
