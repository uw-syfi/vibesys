/**
 * What the chat panel is given to draw: the panel's context, the thread on screen, its items
 * (messages, and a live card for each steer) and what each control does. The effects build it
 * from the run fold and the machines; the UI only draws it.
 */
import type {CardView} from './card-view.js';
import type {PanelContext, PanelEvent} from './panel.js';
import type {SteerDraft, SteerMode, SteerTarget} from './steer.js';
import type {ThreadEntry, ThreadItem} from './thread-view.js';

/** What a card's controls do; each is one event of the card's machine. */
export interface CardActions {
  editText(text: string): void;
  setTarget(target: SteerTarget): void;
  setMode(mode: SteerMode): void;
  send(): void;
  dismiss(): void;
  withdraw(): void;
}

/** A steer card as the panel lists it: its draft and view, and what its controls do. */
export interface CardItem {
  readonly id: string;
  readonly draft: SteerDraft;
  readonly view: CardView;
  readonly actions: CardActions;
}

export type PanelItem =
  | Exclude<ThreadItem, {readonly kind: 'proposal'}>
  | {readonly kind: 'card'; readonly card: CardItem};

export interface ChatPanelView {
  readonly panel: PanelContext;
  /** A thread is being created or a question answered. */
  readonly busy: boolean;
  readonly threads: readonly ThreadEntry[];
  /** The thread on screen; null for a new chat. */
  readonly shown: string | null;
  readonly items: readonly PanelItem[];
  /** The run's roles, in its order: what a steer's target select lists after the whole run. */
  readonly roles: readonly string[];
  /** The run ended: nothing is written or sent to it, so a steer card cannot send. */
  readonly readOnly: boolean;
  /**
   * A question can be asked and a chat opened: on a live run always, on an ended one only when the
   * machine's reader answers over the saved run. Without it the panel only shows the threads.
   */
  readonly canAsk: boolean;
  readonly dispatch: (event: PanelEvent) => void;
}
