/**
 * What a steer card says about itself: its phase, one status line with a tone, and which controls
 * it offers. Pure over the card machine's snapshot, so
 * every state the card can be in has its words in one place.
 */
import {type DeliveryState, deliveryEnded} from './delivery.js';
import type {ChatFailure} from './failure.js';
import {draftProblem, modeLabel, type SteerDraft, targetName} from './steer.js';
import {inForce, type SteerCardContext} from './steer-card.js';

export type CardTone = 'neutral' | 'live' | 'ok' | 'warn' | 'err';

export interface CardStatus {
  readonly label: string;
  readonly tone: CardTone;
}

export type CardPhase = 'draft' | 'sending' | 'sent' | 'withdrawing' | 'dismissed';

export interface CardView {
  readonly phase: CardPhase;
  /** The one status line, for example `In force · Implementer · every turn`; null while a draft. */
  readonly status: CardStatus | null;
  /** Why Send is off, as a short word; null when the draft can be sent. */
  readonly sendBlocked: string | null;
  readonly withdrawable: boolean;
  readonly error: ChatFailure | null;
}

/** The machine value, narrowed to what the view needs. */
export type CardValue = 'draft' | 'sending' | 'dismissed' | {readonly sent: string};

function deliveryStatus(delivery: DeliveryState, draft: SteerDraft): CardStatus {
  const standing = draft.mode === 'standing';
  switch (delivery.kind) {
    case 'sent':
      return {label: 'Sent', tone: 'neutral'};
    case 'waiting':
      return {label: 'Waiting', tone: 'neutral'};
    case 'delivered': {
      const lead = standing ? 'In force' : 'Reached';
      const tone = standing ? 'live' : 'ok';
      if (delivery.idea !== null) return {label: `${lead} · idea ${delivery.idea}`, tone};
      const where = delivery.where ?? targetName(draft.target);
      const when = standing ? modeLabel(draft.mode).toLowerCase() : null;
      return {label: [lead, where, when].filter(Boolean).join(' · '), tone};
    }
    case 'dropped':
      return {label: 'Dropped', tone: 'warn'};
    case 'ended':
      return {label: 'Ended', tone: 'neutral'};
    case 'withdrawn':
      return {label: 'Withdrawn', tone: 'neutral'};
    case 'refused':
      return {
        label: delivery.reason === null ? 'Refused' : `Refused: ${delivery.reason}`,
        tone: 'err',
      };
  }
}

export function cardView(context: SteerCardContext, value: CardValue): CardView {
  const error = context.error;
  const base = {
    error,
    sendBlocked: context.readOnly ? 'Read only' : draftProblem(context.draft),
    withdrawable: false,
  };
  if (value === 'draft') {
    return {...base, phase: 'draft', status: null};
  }
  if (value === 'dismissed') return {...base, phase: 'dismissed', status: null};
  if (value === 'sending')
    return {...base, phase: 'sending', status: {label: 'Sending', tone: 'neutral'}};
  const sub = value.sent;
  const ended = deliveryEnded(context.delivery);
  if ((sub === 'withdrawing' || sub === 'withdrawRequested') && !ended) {
    return {...base, phase: 'withdrawing', status: {label: 'Withdrawing', tone: 'warn'}};
  }
  return {
    ...base,
    phase: 'sent',
    status: deliveryStatus(context.delivery, context.draft),
    withdrawable: inForce(context),
  };
}
