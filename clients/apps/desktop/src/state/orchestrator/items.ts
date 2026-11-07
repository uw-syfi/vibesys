/**
 * `itemIs` names every kind of body an item can have in one place, as `messageIs` does for messages,
 * so a reader asks `itemIs(item, 'tool')` and a kind added to the protocol is a compile error here.
 */
import {type Item, oneofIs} from '@vibesys/orchestrator-proto';

type BodyKind = Exclude<Item['body']['case'], undefined>;

/** An item whose body is of kind `K`. */
export type ItemOf<K extends BodyKind> = Item & {
  readonly body: Extract<Item['body'], {readonly case: K}>;
};

/** Every kind of body an item can have. */
const BODY_KINDS = {
  assistant: true,
  reasoning: true,
  prompt: true,
  tool: true,
} as const satisfies Record<BodyKind, true>;

/** Whether `item` has a body of kind `kind`, and so `item.body.value` is that kind's. */
export function itemIs<K extends BodyKind>(item: Item | undefined, kind: K): item is ItemOf<K> {
  return item !== undefined && oneofIs(item.body, kind, BODY_KINDS);
}
