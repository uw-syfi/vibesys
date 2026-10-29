import assert from 'node:assert/strict';
import {test} from 'node:test';
import {create, toBinary} from '@bufbuild/protobuf';
import {DesktopHostsSchema, validatedExactFromBinary, validatedFromBinary} from '@vibesys/proto';

test('closed config inputs reject unknown fields at root and nested paths; retained readers preserve them', () => {
  for (const nested of [false, true]) {
    const message = create(DesktopHostsSchema, {
      version: 1,
      hosts: {local: {checkout: '/checkout'}},
    });
    const target = nested ? message.hosts['local'] : message;
    if (target === undefined) throw new Error('missing host');
    target.$unknown = [{no: 99, wireType: 0, data: new Uint8Array([1])}];
    const bytes = toBinary(DesktopHostsSchema, message);
    assert.throws(
      () => validatedExactFromBinary(DesktopHostsSchema, bytes),
      nested ? /hosts.local.#99/ : /#99/,
    );
    assert.deepEqual(
      toBinary(DesktopHostsSchema, validatedFromBinary(DesktopHostsSchema, bytes)),
      bytes,
    );
  }
});

test('gateway fixture bytes have the same schema validation as Python', async () => {
  const {readFile} = await import('node:fs/promises');
  const {DesktopGatewayRequestSchema, DesktopGatewayReplySchema} = await import('@vibesys/proto');
  const cases = JSON.parse(
    await readFile(
      new URL('../../../../proto/testdata/desktop/gateway.json', import.meta.url),
      'utf8',
    ),
  );
  for (const item of cases) {
    const schema =
      item.schema === 'request' ? DesktopGatewayRequestSchema : DesktopGatewayReplySchema;
    const bytes = new Uint8Array(Buffer.from(item.protobufHex, 'hex'));
    if (item.valid)
      assert.deepEqual(toBinary(schema, validatedExactFromBinary(schema, bytes)), bytes, item.name);
    else assert.throws(() => validatedExactFromBinary(schema, bytes), undefined, item.name);
  }
});
