import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {test} from 'node:test';
import {AgentLoginSchema, validatedFromBinary} from '@vibesys/proto';

const cases = JSON.parse(
  readFileSync(new URL('../../../../proto/testdata/operations/login-status.json', import.meta.url)),
);
for (const item of cases) {
  test(`login status contract: ${item.name}`, () => {
    const bytes = new Uint8Array(Buffer.from(item.protobufHex, 'hex'));
    if (item.valid) validatedFromBinary(AgentLoginSchema, bytes);
    else assert.throws(() => validatedFromBinary(AgentLoginSchema, bytes));
  });
}
