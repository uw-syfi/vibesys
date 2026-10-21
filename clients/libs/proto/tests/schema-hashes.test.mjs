import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {test} from 'node:test';
import {create, toBinary} from '@bufbuild/protobuf';
import {BinaryWriter} from '@bufbuild/protobuf/wire';
import {
  DescriptorProtoSchema,
  FieldDescriptorProtoSchema,
  FileDescriptorSetSchema,
} from '@bufbuild/protobuf/wkt';
import {API_SCHEMA_HASH, STORE_SCHEMA_HASH, schemaHash} from '../dist/index.js';

test('public schema identities are SHA-256 digests', () => {
  for (const hash of [API_SCHEMA_HASH, STORE_SCHEMA_HASH]) assert.match(hash, /^[0-9a-f]{64}$/);
  assert.notEqual(API_SCHEMA_HASH, STORE_SCHEMA_HASH);
});

test('canonical descriptor order follows declarations, preserving nested and unknown fields', () => {
  const nested = create(DescriptorProtoSchema, {
    name: 'Nested',
    options: {deprecated: true},
    oneofDecl: [{name: 'choice'}],
  });
  const input = create(DescriptorProtoSchema, {
    name: 'Outer',
    nestedType: [nested],
    extension: [create(FieldDescriptorProtoSchema, {name: 'extension', number: 100})],
  });
  const extension = toBinary(FieldDescriptorProtoSchema, input.extension[0]);
  const unknown = new BinaryWriter()
    .tag(1000, 2)
    .bytes(new Uint8Array([1, 2]))
    .finish();
  const encoded = new BinaryWriter()
    .raw(toBinary(DescriptorProtoSchema, input))
    .raw(unknown)
    .finish();
  const expectedNested = new BinaryWriter()
    .tag(1, 2)
    .string('Nested')
    .tag(8, 2)
    .bytes(new BinaryWriter().tag(1, 2).string('choice').finish())
    .tag(7, 2)
    .bytes(new BinaryWriter().tag(3, 0).bool(true).finish())
    .finish();
  const expected = new BinaryWriter()
    .tag(1, 2)
    .string('Outer')
    .tag(6, 2)
    .bytes(extension)
    .tag(3, 2)
    .bytes(expectedNested)
    .raw(unknown)
    .finish();
  const file = message =>
    new BinaryWriter()
      .tag(1, 2)
      .string('test.proto')
      .tag(2, 2)
      .string('test.v1')
      .tag(4, 2)
      .bytes(message)
      .finish();
  const image = message => new BinaryWriter().tag(1, 2).bytes(file(message)).finish();
  const digest = createHash('sha256').update(image(expected)).digest('hex');
  assert.equal(schemaHash(image(encoded), 'test.v1'), digest);
  assert.equal(schemaHash(image(expected), 'test.v1'), digest);
});

test('package identity ignores comments, other packages and file order, but includes validation options', () => {
  const file = name => ({
    name,
    package: 'test.v1',
    syntax: 'proto3',
    messageType: [{name: 'Message', options: {deprecated: true}}],
  });
  const a = file('a.proto');
  const b = file('b.proto');
  const hash = files =>
    schemaHash(
      toBinary(FileDescriptorSetSchema, create(FileDescriptorSetSchema, {file: files})),
      'test.v1',
    );
  const expected = hash([a, b]);
  assert.equal(
    hash([
      {...b, sourceCodeInfo: {location: [{leadingComments: 'changed'}]}},
      a,
      {name: 'other.proto', package: 'other.v1'},
    ]),
    expected,
  );
  assert.notEqual(hash([a, {...b, messageType: [{name: 'Message'}]}]), expected);
  assert.throws(() => schemaHash(new Uint8Array(), 'missing.v1'), /missing.v1/);
});
