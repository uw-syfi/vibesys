import type {DescMessage} from '@bufbuild/protobuf';
import {BinaryReader, BinaryWriter, WireType} from '@bufbuild/protobuf/wire';

/** Preserve protobuf-py's descriptor declaration order and opaque option bytes. */
export function canonicalDescriptor(schema: DescMessage, bytes: Uint8Array): Uint8Array {
  const reader = new BinaryReader(bytes);
  const fields = new Map(schema.fields.map((field, index) => [field.number, {field, index}]));
  const chunks: {order: number; bytes: Uint8Array}[] = [];
  while (reader.pos < reader.len) {
    const start = reader.pos;
    const [number, wire] = reader.tag();
    const known = fields.get(number);
    let chunk: Uint8Array;
    if (wire === WireType.LengthDelimited && known?.field.message) {
      chunk = new BinaryWriter()
        .tag(number, wire)
        .bytes(canonicalDescriptor(known.field.message, reader.bytes()))
        .finish();
    } else {
      reader.skip(wire, number);
      chunk = bytes.subarray(start, reader.pos);
    }
    chunks.push({order: known?.index ?? schema.fields.length, bytes: chunk});
  }
  const writer = new BinaryWriter();
  for (const chunk of chunks.sort((left, right) => left.order - right.order))
    writer.raw(chunk.bytes);
  return writer.finish();
}
