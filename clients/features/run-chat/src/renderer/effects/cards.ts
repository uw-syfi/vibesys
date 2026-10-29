/**
 * The steer cards of one run chat: one `steerCardMachine` actor per card, created when its
 * proposal (or the steer the user wrote) first appears and kept for as long as the chat lives, so
 * a card keeps its edits when the panel is closed or another thread is shown. The control
 * commands a card sends go through the injected `control` port; control ids come from
 * `newControlId`. Views subscribe through `subscribe` and `version`.
 */
import {type Actor, createActor, fromPromise} from 'xstate';
import {
  type ControlDelivery,
  type ControlOutcome,
  type DeliverRequest,
  type SteerCardEvent,
  type SteerCardInput,
  steerCardMachine,
} from '../../state/index.js';
import type {RunChatPorts} from './ports.js';

type CardActor = Actor<typeof steerCardMachine>;
export type CardSnapshot = ReturnType<CardActor['getSnapshot']>;

export class SteerCards {
  readonly #actors = new Map<string, CardActor>();
  readonly #listeners = new Set<() => void>();
  readonly #machine: typeof steerCardMachine;
  #version = 0;

  constructor(private readonly ports: Pick<RunChatPorts, 'control' | 'newControlId'>) {
    this.#machine = steerCardMachine.provide({
      actors: {
        deliver: fromPromise<ControlOutcome, {readonly request: DeliverRequest}>(({input}) =>
          this.ports.control(input.request),
        ),
      },
    });
  }

  /** Creates the card `input.id` names if there is none; a card that exists is left as it is. */
  ensure(input: SteerCardInput): void {
    if (this.#actors.has(input.id)) return;
    const actor = createActor(this.#machine, {input});
    actor.start();
    // After the start, so creating a card while a view renders tells that view nothing.
    actor.subscribe(() => this.#changed());
    this.#actors.set(input.id, actor);
  }

  snapshot(id: string): CardSnapshot | null {
    return this.#actors.get(id)?.getSnapshot() ?? null;
  }

  send(id: string, event: SteerCardEvent): void {
    this.#actors.get(id)?.send(event);
  }

  /** Tells every card whether the run can take a steer; a card that cannot keeps its draft. */
  setReadOnly(value: boolean): void {
    for (const actor of this.#actors.values()) actor.send({type: 'read-only', value});
  }

  /** Sends the card its draft as a steer, under a new control id. */
  sendSteer(id: string): void {
    this.send(id, {type: 'send', controlId: this.ports.newControlId()});
  }

  /** Asks to end the standing steer the card sent, under a new control id. */
  withdraw(id: string): void {
    this.send(id, {type: 'withdraw', controlId: this.ports.newControlId()});
  }

  /** Reports what the server said about controls; each card takes only its own. */
  deliver(deliveries: readonly ControlDelivery[]): void {
    for (const {controlId, event} of deliveries) {
      for (const actor of this.#actors.values()) {
        actor.send({type: 'delivery', controlId, event});
      }
    }
  }

  readonly subscribe = (listener: () => void): (() => void) => {
    this.#listeners.add(listener);
    return () => this.#listeners.delete(listener);
  };

  readonly version = (): number => this.#version;

  stop(): void {
    for (const actor of this.#actors.values()) actor.stop();
    this.#actors.clear();
  }

  #changed(): void {
    this.#version += 1;
    for (const listener of [...this.#listeners]) listener();
  }
}
