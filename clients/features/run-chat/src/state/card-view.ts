/**
 * What a steer card says about itself: its phase, one status word with a tone, the summary of
 * what it will do or did, and which controls it offers. Pure over the card machine's snapshot, so
 * every state the card can be in has its words in one place.
 */
import type {DeliveryState} from './delivery.js';
import {draftProblem, modeLabel, type SteerDraft, targetLabel} from './steer.js';
import {inForce, type SteerCardContext} from './steer-card.js';

export type CardTone = 'neutral' | 'live' | 'ok' | 'warn' | 'err';

export interface CardStatus {
  readonly label: string;
  readonly tone: CardTone;
}

export type CardPhase = 'draft' | 'sending' | 'sent' | 'withdrawing' | 'dismissed';

export interface CardView {
  readonly phase: CardPhase;
  /** The status chip; null while the card is still a draft. */
  readonly status: CardStatus | null;
  /** To and When as the card shows them once sent, for example `Role implementer · Every turn`. */
  readonly summary: string;
  /** Why Send is off, as a short word; null when the draft can be sent. */
  readonly sendBlocked: string | null;
  readonly withdrawable: boolean;
  readonly error: string | null;
}

/** The machine value, narrowed to what the view needs. */
export type CardValue = 'draft' | 'sending' | 'dismissed' | {readonly sent: string};

function deliveryStatus(delivery: DeliveryState, standing: boolean): CardStatus {
  switch (delivery.kind) {
    case 'sent':
      return {label: 'Sent', tone: 'neutral'};
    case 'waiting':
      return {label: 'Waiting', tone: 'neutral'};
    case 'delivered': {
      const lead = standing ? 'In force' : 'Reached';
      const tone = standing ? 'live' : 'ok';
      if (delivery.idea !== null) {
        return {label: `${lead}${standing ? ' from' : ''} idea ${delivery.idea}`, tone};
      }
      return {label: delivery.where === null ? lead : `${lead} · ${delivery.where}`, tone};
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

/** To and When in the words the card's controls use. */
export function steerSummary(draft: SteerDraft): string {
  const {target} = draft;
  const to =
    target.kind === 'run' ? targetLabel('run') : `${targetLabel(target.kind)} ${target.id ?? ''}`;
  return `${to.trim()} · ${modeLabel(draft.mode)}`;
}

export function cardView(context: SteerCardContext, value: CardValue): CardView {
  const summary = steerSummary(context.draft);
  const error = context.error;
  const base = {summary, error, sendBlocked: draftProblem(context.draft), withdrawable: false};
  if (value === 'draft') {
    return {...base, phase: 'draft', status: null};
  }
  if (value === 'dismissed') return {...base, phase: 'dismissed', status: null};
  if (value === 'sending')
    return {...base, phase: 'sending', status: {label: 'Sending', tone: 'neutral'}};
  const sub = value.sent;
  if (sub === 'withdrawing' || sub === 'withdrawRequested') {
    return {...base, phase: 'withdrawing', status: {label: 'Withdrawing', tone: 'warn'}};
  }
  return {
    ...base,
    phase: 'sent',
    status: deliveryStatus(context.delivery, context.draft.mode === 'standing'),
    withdrawable: inForce(context),
  };
}
