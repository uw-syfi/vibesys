import assert from 'node:assert/strict';
import test from 'node:test';
import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import {FileDescriptorSetSchema} from '@bufbuild/protobuf/wkt';
import {descriptorRegistry, WorkstreamAttrsSchema} from '@vibesys/proto';

function descriptorSet() {
  const files = new Map();
  const add = file => {
    if (files.has(file.name)) return;
    files.set(file.name, file);
    for (const dependency of file.dependencies) add(dependency);
  };
  add(WorkstreamAttrsSchema.file);
  return create(FileDescriptorSetSchema, {file: [...files.values()].map(file => file.proto)});
}
test('descriptor order, copies and repeated lookups preserve the declared message', () => {
  const descriptors = descriptorSet();
  const expected = create(WorkstreamAttrsSchema, {hypothesisId: 'idea-a', revision: 'revision-a'});
  for (let rotation = 0; rotation < descriptors.file.length; rotation += 1) {
    const shuffled = create(FileDescriptorSetSchema, {
      file: [...descriptors.file.slice(rotation), ...descriptors.file.slice(0, rotation)],
    });
    const schema = descriptorRegistry(shuffled).getMessage(WorkstreamAttrsSchema.typeName);
    assert.ok(schema);
    assert.deepEqual(fromBinary(schema, toBinary(WorkstreamAttrsSchema, expected)), expected);
    assert.equal(descriptorRegistry(shuffled), descriptorRegistry(shuffled));
  }
});
test('missing, duplicate and cyclic descriptor files are rejected by name', () => {
  const missing = create(FileDescriptorSetSchema, {
    file: [{name: 'root.proto', syntax: 'proto3', dependency: ['missing.proto']}],
  });
  assert.throws(() => descriptorRegistry(missing), /missing.proto/);
  const duplicate = create(FileDescriptorSetSchema, {
    file: [{name: 'root.proto'}, {name: 'root.proto'}],
  });
  assert.throws(() => descriptorRegistry(duplicate), /duplicate/);
  const cycle = create(FileDescriptorSetSchema, {
    file: [
      {name: 'a.proto', dependency: ['b.proto']},
      {name: 'b.proto', dependency: ['a.proto']},
    ],
  });
  assert.throws(() => descriptorRegistry(cycle), /cycle/);
});
