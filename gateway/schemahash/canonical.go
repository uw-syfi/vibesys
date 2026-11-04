package schemahash

import (
	"sort"

	"google.golang.org/protobuf/encoding/protowire"
	"google.golang.org/protobuf/reflect/protoreflect"
)

// canonicalDescriptor preserves protobuf-py's descriptor declaration order.
// Unknown options remain opaque, retaining their source encoding.
func canonicalDescriptor(schema protoreflect.MessageDescriptor, encoded []byte) []byte {
	type chunk struct {
		order   int
		encoded []byte
	}
	var chunks []chunk
	fields := schema.Fields()
	for len(encoded) > 0 {
		number, wire, tagLen := protowire.ConsumeTag(encoded)
		if tagLen < 0 {
			panic(protowire.ParseError(tagLen))
		}
		valueLen := protowire.ConsumeFieldValue(number, wire, encoded[tagLen:])
		if valueLen < 0 {
			panic(protowire.ParseError(valueLen))
		}
		end := tagLen + valueLen
		raw := encoded[:end]
		field := fields.ByNumber(number)
		order := fields.Len()
		if field != nil {
			order = field.Index()
			if wire == protowire.BytesType && field.Message() != nil {
				value, n := protowire.ConsumeBytes(encoded[tagLen:])
				if n < 0 {
					panic(protowire.ParseError(n))
				}
				raw = protowire.AppendTag(nil, number, wire)
				raw = protowire.AppendBytes(raw, canonicalDescriptor(field.Message(), value))
			}
		}
		chunks = append(chunks, chunk{order, raw})
		encoded = encoded[end:]
	}
	sort.SliceStable(chunks, func(i, j int) bool { return chunks[i].order < chunks[j].order })
	var result []byte
	for _, chunk := range chunks {
		result = append(result, chunk.encoded...)
	}
	return result
}
