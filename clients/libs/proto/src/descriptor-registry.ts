/** Bounded memo of immutable descriptor sets. Descriptor order has no semantic meaning. */
import {create, createFileRegistry, type FileRegistry, toBinary} from '@bufbuild/protobuf';
import {
  type FileDescriptorProto,
  type FileDescriptorSet,
  FileDescriptorSetSchema,
} from '@bufbuild/protobuf/wkt';
import {sha256} from '@noble/hashes/sha2.js';
import {bytesToHex} from '@noble/hashes/utils.js';

const registries = new Map<string, FileRegistry>();
export function descriptorRegistry(descriptors: FileDescriptorSet): FileRegistry {
  const hash = bytesToHex(sha256(toBinary(FileDescriptorSetSchema, descriptors)));
  const cached = registries.get(hash);
  if (cached) return cached;
  const files = new Map(descriptors.file.map(file => [file.name, file]));
  if (files.size !== descriptors.file.length) throw new Error('descriptors: duplicate file name');
  const ordered: FileDescriptorProto[] = [];
  const visiting = new Set<string>();
  const visited = new Set<string>();
  const visit = (file: FileDescriptorProto): void => {
    if (visiting.has(file.name)) throw new Error(`descriptors: cycle at ${file.name}`);
    if (visited.has(file.name)) return;
    visiting.add(file.name);
    for (const name of file.dependency) {
      const dependency = files.get(name);
      if (!dependency) throw new Error(`descriptors: missing ${name}`);
      visit(dependency);
    }
    visiting.delete(file.name);
    visited.add(file.name);
    ordered.push(file);
  };
  for (const file of descriptors.file) visit(file);
  const registry = createFileRegistry(create(FileDescriptorSetSchema, {file: ordered}));
  registries.set(hash, registry);
  if (registries.size > 16) {
    const oldest = registries.keys().next().value;
    if (oldest !== undefined) registries.delete(oldest);
  }
  return registry;
}
