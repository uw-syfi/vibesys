import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {test} from 'node:test';
import {create, fromJson, toBinary} from '@bufbuild/protobuf';
import * as proto from '@vibesys/proto';

function expand(value) {
  if (Array.isArray(value)) return value.map(expand);
  if (value && typeof value === 'object') {
    if ('$bytes' in value) return Buffer.alloc(value.$bytes).toString('base64');
    if ('$string' in value) return 'x'.repeat(value.$string);
    if ('$repeat' in value) return Array.from({length: value.$repeat}, () => expand(value.value));
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, expand(item)]));
  }
  return value;
}

const cases = JSON.parse(
  readFileSync(new URL('../../../../proto/testdata/host-validation.json', import.meta.url)),
);
const schemas = new Map(
  [
    ...proto.HostService.methods.flatMap(method => [method.input, method.output]),
    proto.BlobResultSchema,
    proto.RunEntrySchema,
  ].map(schema => [schema.typeName, schema]),
);
for (const item of cases) {
  test(item.name, () => {
    const schema = schemas.get(`vibesys.api.v1.${item.message}`);
    const bytes = toBinary(schema, fromJson(schema, expand(item.value)));
    if (item.field) {
      assert.throws(
        () => proto.validatedFromBinary(schema, bytes),
        error => {
          // Standard field paths use protobuf names; CEL rules carry their stable rule id.
          assert.ok(String(error).includes(item.field), String(error));
          return true;
        },
      );
    } else {
      proto.validatedFromBinary(schema, bytes);
    }
  });
}

test('generic bridge validates methods and both payload directions', () => {
  const method = `/${proto.HostService.typeName}/Hello`;
  const bytes = toBinary(proto.HelloRequestSchema, create(proto.HelloRequestSchema));
  assert.throws(() => proto.validateHostPayload(method, 'input', bytes), /api_schema_hash/);
  assert.throws(() => proto.validateHostPayload(method, 'output', bytes), /api_schema_hash/);
  assert.throws(
    () => proto.validateHostPayload('/unknown', 'input', bytes),
    /unknown HostService method/,
  );
});

test('generated valid run ids round trip; forbidden path characters fail at every position', () => {
  const schemas = [
    proto.TailRequestSchema,
    proto.SkeletonRequestSchema,
    proto.GetBlobsRequestSchema,
    proto.TailStreamRequestSchema,
  ];
  for (let length = 1; length <= 128; length += 1) {
    const runId = `r${'a_9-'.repeat(32)}`.slice(0, length);
    for (const schema of schemas) {
      const message = create(schema, {runId, streamId: 'output'});
      proto.validatedFromBinary(schema, toBinary(schema, message));
      for (const forbidden of ['/', '.', ' ', '\0', '\n']) {
        message.runId = runId.slice(0, length - 1) + forbidden;
        assert.throws(() => proto.validatedFromBinary(schema, toBinary(schema, message)), /run_id/);
      }
    }
  }
});
