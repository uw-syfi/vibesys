import {strict as assert} from 'node:assert';
import {afterEach, test} from 'node:test';
import {copyText} from './clipboard.js';

const original = (globalThis as {navigator?: Navigator}).navigator;

afterEach(() => {
  Object.defineProperty(globalThis, 'navigator', {value: original, configurable: true});
});

const withClipboard = (writeText: (text: string) => Promise<void>) => {
  Object.defineProperty(globalThis, 'navigator', {
    value: {clipboard: {writeText}},
    configurable: true,
  });
};

test('a clipboard write that resolves reports success', async () => {
  withClipboard(() => Promise.resolve());
  assert.equal(await copyText('hi'), true);
});

test('a clipboard write that rejects reports failure instead of throwing', async () => {
  withClipboard(() => Promise.reject(new Error('denied')));
  await assert.doesNotReject(async () => {
    assert.equal(await copyText('hi'), false);
  });
});
