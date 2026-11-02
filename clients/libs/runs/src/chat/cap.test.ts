import {expect, test} from 'bun:test';
import {CHAT_TEXT_MAX_BYTES} from './index.js';

test("a chat question's cap is the contract's, in UTF-8 bytes", () => {
  expect(CHAT_TEXT_MAX_BYTES).toBe(65536);
});
