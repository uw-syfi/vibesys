/** Author the existing UI scenarios as dense native session records at the wire boundary. */
import type {Page} from '@playwright/test';
import type {RunEvent} from '@vibesys/contracts';
import type {Record as StoreRecord} from '@vibesys/proto';
import {type ChatHistories, chatBatches, tailBytes} from '../src/testing/chat-records.js';

const histories = new WeakMap<Page, ChatHistories>();

export async function emitChat(page: Page, events: readonly RunEvent[]): Promise<void> {
  const sessions = histories.get(page) ?? new Map<string, StoreRecord[]>();
  histories.set(page, sessions);
  for (const [id, batch] of chatBatches(sessions, events)) {
    if (batch.length === 0) continue;
    await page.evaluate(
      ({id, bytes}) =>
        (
          window as unknown as {fakeGateway: {seedChat(id: string, bytes: number[]): void}}
        ).fakeGateway.seedChat(id, bytes),
      {id, bytes: tailBytes(batch)},
    );
  }
}
