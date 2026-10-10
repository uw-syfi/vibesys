import {expect, type Page} from '@playwright/test';
import {MatrixGatewayOwner, type MatrixGatewayResource, matrixTest as test} from './gateway.js';

interface EventBatchFrame {
  readonly type: 'event_batch';
  readonly store_id?: string;
  readonly through_sequence?: number;
  readonly events: readonly unknown[];
}

/** Causal view of public event batches received by every page WebSocket. */
class BatchProbe {
  readonly #queued: EventBatchFrame[] = [];
  readonly #waiters: Array<{
    readonly predicate: (batch: EventBatchFrame) => boolean;
    readonly resolve: (batch: EventBatchFrame) => void;
  }> = [];

  constructor(page: Page) {
    page.on('websocket', socket => {
      socket.on('framereceived', frame => this.#receive(frame.payload));
    });
  }

  next(predicate: (batch: EventBatchFrame) => boolean = () => true): Promise<EventBatchFrame> {
    const index = this.#queued.findIndex(predicate);
    if (index >= 0) {
      const [batch] = this.#queued.splice(index, 1);
      if (batch !== undefined) return Promise.resolve(batch);
    }
    return new Promise(resolve => this.#waiters.push({predicate, resolve}));
  }

  #receive(frame: string | Buffer): void {
    let value: unknown;
    try {
      value = JSON.parse(typeof frame === 'string' ? frame : frame.toString('utf8'));
    } catch {
      return;
    }
    if (!isEventBatch(value)) return;
    const waiterIndex = this.#waiters.findIndex(waiter => waiter.predicate(value));
    if (waiterIndex < 0) {
      this.#queued.push(value);
      return;
    }
    const [waiter] = this.#waiters.splice(waiterIndex, 1);
    waiter?.resolve(value);
  }
}

test('gateway restart on the same port replaces the fold with the new store', async ({
  page,
  matrixGateways,
}) => {
  const oldMarker = 'matrix-store-before-restart';
  const newMarker = 'matrix-store-after-restart';
  const first = await matrixGateways.start(oldMarker);
  const batches = new BatchProbe(page);
  const initialBatch = batches.next(batch => batchContains(batch, oldMarker));

  await page.goto(first.url);
  const before = await initialBatch;
  expect(before.store_id).toBeTruthy();
  await expect(page.getByText(oldMarker, {exact: false})).toBeVisible();

  await first.crash();
  const replacement = await matrixGateways.start(newMarker, first.port);
  expect(replacement.port).toBe(first.port);
  await installCapability(page, replacement.url);

  const replacementBatch = batches.next(batch => batchContains(batch, newMarker));
  await page.evaluate(() => window.dispatchEvent(new Event('online')));
  const after = await replacementBatch;

  expect(after.store_id).toBeTruthy();
  expect(after.store_id).not.toBe(before.store_id);
  await expect(page.getByText(oldMarker, {exact: false})).toHaveCount(0);
  await expect(page.getByText(newMarker, {exact: false})).toBeVisible();
  await expect(page.locator('.status')).toHaveText('running');
});

test('page reload keeps the live run and a fresh client converges to the same state', async ({
  page,
  matrixGateways,
}) => {
  const marker = 'matrix-live-page-reload';
  const gateway = await matrixGateways.start(marker);
  const batches = new BatchProbe(page);
  const initialBatch = batches.next(batch => batchContains(batch, marker));

  await page.goto(gateway.url);
  const before = await initialBatch;
  await expect(page.getByText(marker, {exact: false})).toBeVisible();
  await expect(page.locator('.status')).toHaveText('running');
  const sequence = await sequenceText(page);
  const transcript = await page.locator('.panel-heading span').textContent();
  expect(gateway.isRunning()).toBe(true);

  const reloadedBatch = batches.next(batch => batch.store_id === before.store_id);
  await page.reload();
  const after = await reloadedBatch;

  expect(gateway.isRunning()).toBe(true);
  expect(after.store_id).toBe(before.store_id);
  expect(after.through_sequence).toBe(before.through_sequence);
  await expect(page.getByText(marker, {exact: false})).toBeVisible();
  await expect(page.locator('.status')).toHaveText('running');
  expect(await sequenceText(page)).toBe(sequence);
  expect(await page.locator('.panel-heading span').textContent()).toBe(transcript);
});

test('worker exit terminates every gateway and retains an unconfirmed runtime directory', async () => {
  const removed: string[] = [];
  const owner = new MatrixGatewayOwner(() => removed.push('removed'));
  const failedSignal = owner.track(new FakeMatrixGatewayResource(false, true));
  const remainingGateway = owner.track(new FakeMatrixGatewayResource(false));

  owner.terminateForWorkerExit();
  expect(failedSignal.emergencyTerminations).toBe(1);
  expect(remainingGateway.emergencyTerminations).toBe(1);
  expect(removed).toEqual([]);

  const cleanup = await owner.dispose();
  expect(failedSignal.disposals).toBe(1);
  expect(remainingGateway.disposals).toBe(1);
  expect(failedSignal.emergencyTerminations).toBe(2);
  expect(remainingGateway.emergencyTerminations).toBe(2);
  expect(cleanup).toEqual({
    errors: ['runtime directory retained because child termination was not confirmed'],
    runtimeDirectoryRemoved: false,
    terminationConfirmed: false,
  });
  expect(removed).toEqual([]);
});

class FakeMatrixGatewayResource implements MatrixGatewayResource {
  disposals = 0;
  emergencyTerminations = 0;

  readonly #terminated: boolean;
  readonly #throwOnEmergency: boolean;

  constructor(terminated: boolean, throwOnEmergency = false) {
    this.#terminated = terminated;
    this.#throwOnEmergency = throwOnEmergency;
  }

  dispose(): Promise<void> {
    this.disposals += 1;
    return Promise.resolve();
  }

  terminateForWorkerExit(): void {
    this.emergencyTerminations += 1;
    if (this.#throwOnEmergency) throw new Error('signal refused');
  }

  terminationConfirmed(): boolean {
    return this.#terminated;
  }
}

function isEventBatch(value: unknown): value is EventBatchFrame {
  return (
    typeof value === 'object' &&
    value !== null &&
    (value as {type?: unknown}).type === 'event_batch' &&
    Array.isArray((value as {events?: unknown}).events)
  );
}

function batchContains(batch: EventBatchFrame, marker: string): boolean {
  return batch.events.some(event => {
    if (typeof event !== 'object' || event === null) return false;
    const record = event as {text?: unknown; data?: unknown};
    if (typeof record.text === 'string' && record.text.includes(marker)) return true;
    if (typeof record.data !== 'object' || record.data === null) return false;
    const content = (record.data as {content?: unknown}).content;
    return typeof content === 'string' && content.includes(marker);
  });
}

async function installCapability(page: Page, url: string): Promise<void> {
  await page.evaluate(async capability => {
    const response = await fetch(capability, {cache: 'no-store', credentials: 'include'});
    if (!response.ok) throw new Error(`Capability exchange failed with ${response.status}`);
  }, url);
}

async function sequenceText(page: Page): Promise<string | null> {
  return page
    .locator('.summary > div')
    .filter({hasText: 'Sequence'})
    .locator('strong')
    .textContent();
}
