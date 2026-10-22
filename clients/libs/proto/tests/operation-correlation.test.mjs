import assert from 'node:assert/strict';
import {test} from 'node:test';
import {create, toBinary} from '@bufbuild/protobuf';
import {BytesValueSchema} from '@bufbuild/protobuf/wkt';
import {accept, call} from '../dist/operations.js';

const policy = {name: 'ByteCorrelation', id: 1, capMs: 10, graceMs: 5, totalMs: 15};
const definition = {
  policy,
  requestSchema: BytesValueSchema,
  responseSchema: BytesValueSchema,
  correlations: [[['value'], ['value']]],
};

test('byte correlations compare content across independently decoded messages', async () => {
  const transport = {
    async invoke(_id, payload) {
      return payload.slice();
    },
  };
  for (const value of [new Uint8Array(), new Uint8Array([0, 255]), new Uint8Array(32).fill(7)]) {
    const request = accept(policy, BytesValueSchema, {value});
    const response = await call(transport, definition, request, new AbortController().signal);
    assert.deepEqual(response.value, value);
  }
});

test('byte correlations reject different content and lengths', async () => {
  const request = accept(policy, BytesValueSchema, {value: new Uint8Array([0, 255])});
  for (const value of [new Uint8Array([0, 254]), new Uint8Array([0]), new Uint8Array()]) {
    const transport = {
      async invoke() {
        return toBinary(BytesValueSchema, create(BytesValueSchema, {value}));
      },
    };
    await assert.rejects(
      call(transport, definition, request, new AbortController().signal),
      /correlation/,
    );
  }
});
