/**
 * The same steps run over Electron's MessageChannelMain between two processes in
 * desktop-main/e2e/gateway.spec.ts: the receiver closes first or not, the sender posts every message
 * and closes at once, and what the receiver got is compared.
 */
import {expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {FakeGatewayPort} from './testing/index.js';

const cases: Array<{
  name: string;
  messages: number[][];
  receiverClosed: boolean;
  expected: number[][];
}> = JSON.parse(
  readFileSync(
    new URL('../../../../proto/testdata/gateway_port/contract.json', import.meta.url),
    'utf8',
  ),
);
for (const row of cases)
  test(`Fake gateway port contract: ${row.name}`, () => {
    const tasks: Array<() => void> = [];
    const flush = (): void => {
      while (tasks.length) tasks.shift()?.();
    };
    const {main, page} = FakeGatewayPort.pair((_lane, task) => tasks.push(task), structuredClone);
    const received: number[][] = [];
    const sawClose = {sender: 0, receiver: 0};
    // The sender is the main process; the receiver is the page, another process.
    page.onmessage = event => received.push([...(event.data as Uint8Array)]);
    page.on('close', () => {
      sawClose.receiver += 1;
    });
    main.on('close', () => {
      sawClose.sender += 1;
    });
    if (row.receiverClosed) {
      page.close();
      flush();
    }
    for (const value of row.messages) {
      const bytes = new Uint8Array(value);
      main.postMessage(bytes);
      bytes.fill(99);
    }
    main.close();
    flush();
    expect(received).toEqual(row.expected);
    expect(sawClose).toEqual(
      row.receiverClosed ? {sender: 1, receiver: 0} : {sender: 0, receiver: 1},
    );
  });
