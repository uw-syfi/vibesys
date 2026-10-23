package runlog

import (
	"errors"
	"io"

	storev1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/store/v1"
	"google.golang.org/protobuf/encoding/protowire"
	"google.golang.org/protobuf/proto"
)

// CurrentEpochNumber reads the canonical EpochStarted declarations in the
// published prefix. A nil committed sequence admits the complete legacy prefix;
// zero admits no records. Process metadata and Record.epoch are never authority.
// Other product bodies stay opaque. The caller must freeze the input extent.
func CurrentEpochNumber(source io.ReaderAt, committed *uint64) (uint32, error) {
	var epoch uint32
	err := ReadPrefix(source, committed, func(payload []byte) error {
		declared, err := DeclaredEpoch(payload)
		epoch = max(epoch, declared)
		return err
	})
	return epoch, err
}

// ReadPrefix passes every record payload of the published prefix to visit, in order.
// It rejects a damaged log, a non-increasing sequence, and a log shorter than its fence.
func ReadPrefix(source io.ReaderAt, committed *uint64, visit func(payload []byte) error) error {
	if committed != nil && *committed == 0 {
		return nil
	}
	reader := NewReader(source)
	var through uint64
	var seen bool
	for {
		payload, err := reader.Next()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			return err
		}
		seq, err := Sequence(payload)
		if err != nil {
			return err
		}
		if seen && seq <= through {
			return &Corruption{Offset: -1, Reason: "non-increasing Record.seq"}
		}
		if committed != nil && seq > *committed {
			break
		}
		if err := visit(payload); err != nil {
			return err
		}
		through, seen = seq, true
		if committed != nil && through == *committed {
			break
		}
	}
	if committed != nil && (!seen || through < *committed) {
		return &Corruption{Offset: -1, Reason: "committed sequence exceeds log prefix"}
	}
	return nil
}

// DeclaredEpoch preserves protobuf oneof selection and embedded-message merging while eliding
// opaque bodies. Only EpochStarted metadata is interpreted by this reader.
func DeclaredEpoch(payload []byte) (uint32, error) {
	fields := (&storev1.Record{}).ProtoReflect().Descriptor().Oneofs().ByName("body").Fields()
	epochField := (&storev1.Record{}).ProtoReflect().Descriptor().Fields().ByName("epoch_started").Number()
	var filtered []byte
	for len(payload) > 0 {
		number, kind, width := protowire.ConsumeTag(payload)
		if width < 0 {
			return 0, &Corruption{Offset: -1, Reason: "invalid Record field tag"}
		}
		payload = payload[width:]
		n := protowire.ConsumeFieldValue(number, kind, payload)
		if n < 0 {
			return 0, &Corruption{Offset: -1, Reason: "invalid Record field"}
		}
		if kind == protowire.BytesType && fields.ByNumber(number) != nil {
			value, _ := protowire.ConsumeBytes(payload)
			if number != epochField {
				value = nil
			}
			filtered = protowire.AppendTag(filtered, number, kind)
			filtered = protowire.AppendBytes(filtered, value)
		}
		payload = payload[n:]
	}
	record := &storev1.Record{}
	if err := proto.Unmarshal(filtered, record); err != nil {
		return 0, &Corruption{Offset: -1, Reason: "invalid EpochStarted metadata"}
	}
	return record.GetEpochStarted().GetEpoch(), nil
}
