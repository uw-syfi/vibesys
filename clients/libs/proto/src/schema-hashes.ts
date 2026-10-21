import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import {FileDescriptorSetSchema} from '@bufbuild/protobuf/wkt';
import {sha256} from '@noble/hashes/sha2.js';
import {bytesToHex} from '@noble/hashes/utils.js';
import {canonicalDescriptor} from './canonical-descriptor.js';
import {schemaFiles} from './generated/schema-files.js';

/** Hash a package's source descriptors, preserving options and ignoring source info. */
export function schemaHash(image: Uint8Array, packageName: string): string {
  const descriptorSet = fromBinary(FileDescriptorSetSchema, image);
  descriptorSet.file = descriptorSet.file.filter(file => file.package === packageName);
  if (descriptorSet.file.length === 0) throw new Error(`No descriptors in package ${packageName}`);
  for (const file of descriptorSet.file) file.sourceCodeInfo = undefined;
  descriptorSet.file.sort((left, right) =>
    left.name < right.name ? -1 : left.name > right.name ? 1 : 0,
  );
  return bytesToHex(
    sha256(
      canonicalDescriptor(
        FileDescriptorSetSchema,
        toBinary(FileDescriptorSetSchema, descriptorSet),
      ),
    ),
  );
}

const embeddedImage = toBinary(
  FileDescriptorSetSchema,
  create(FileDescriptorSetSchema, {
    file: schemaFiles.map(file => file.proto),
  }),
);
export const API_SCHEMA_HASH = schemaHash(embeddedImage, 'vibesys.api.v1');
export const STORE_SCHEMA_HASH = schemaHash(embeddedImage, 'vibesys.store.v1');
