package runlog

import (
	storev1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/store/v1"
	"google.golang.org/protobuf/encoding/protowire"
)

var sequenceField = protowire.Number((&storev1.Record{}).ProtoReflect().Descriptor().Fields().ByName("seq").Number())

// Sequence reads only Record's envelope seq (field 1), skipping opaque fields
// without interpreting product payloads. Duplicate seq fields follow protobuf's
// last-value-wins semantics. Missing seq has protobuf's uint64 default, zero.
func Sequence(payload []byte) (uint64, error) {
	seq := uint64(0)
	for len(payload) > 0 {
		number, kind, width := protowire.ConsumeTag(payload)
		if width < 0 {
			return 0, &Corruption{Offset: -1, Reason: "invalid Record field tag"}
		}
		payload = payload[width:]
		if number == sequenceField {
			if kind != protowire.VarintType {
				return 0, &Corruption{Offset: -1, Reason: "Record.seq is not a uint64"}
			}
			value, n := protowire.ConsumeVarint(payload)
			if n < 0 {
				return 0, &Corruption{Offset: -1, Reason: "invalid Record.seq"}
			}
			seq = value
			payload = payload[n:]
		} else {
			n := protowire.ConsumeFieldValue(number, kind, payload)
			if n < 0 {
				return 0, &Corruption{Offset: -1, Reason: "invalid Record field"}
			}
			payload = payload[n:]
		}
	}
	return seq, nil
}
