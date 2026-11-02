import assert from 'node:assert/strict';
import {test} from 'node:test';
import {ChatMessageRequestSchema, stringMaxBytes} from '@vibesys/proto';

test('a string field reads its max_bytes from the contract', () => {
  assert.equal(stringMaxBytes(ChatMessageRequestSchema.field.text), 65536);
});

test('a string field with no max_bytes rule is an error naming the field', () => {
  assert.throws(
    () => stringMaxBytes(ChatMessageRequestSchema.field.requestId),
    /vibesys\.api\.v1\.ChatMessageRequest\.request_id/,
  );
});
