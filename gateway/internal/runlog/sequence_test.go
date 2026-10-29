package runlog_test

import (
	"bytes"
	"errors"
	"testing"
	"testing/quick"

	storev1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/store/v1"
	"github.com/uw-syfi/vibesys/gateway/internal/runlog"
	"google.golang.org/protobuf/encoding/protowire"
	"google.golang.org/protobuf/proto"
)

func TestSequenceMatchesGeneratedEnvelope(t *testing.T) {
	property := func(seq uint64, opaque []byte) bool {
		payload, err := proto.Marshal(&storev1.Record{Seq: seq})
		if err != nil {
			return false
		}
		payload = protowire.AppendTag(payload, 100, protowire.BytesType)
		payload = protowire.AppendBytes(payload, opaque)
		before := bytes.Clone(payload)
		got, err := runlog.Sequence(payload)
		return err == nil && got == seq && bytes.Equal(payload, before)
	}
	if err := quick.Check(property, nil); err != nil {
		t.Fatal(err)
	}
}

func TestSequenceUsesLastValue(t *testing.T) {
	if err := quick.Check(func(first, last uint64) bool {
		payload := protowire.AppendTag(nil, 1, protowire.VarintType)
		payload = protowire.AppendVarint(payload, first)
		payload = protowire.AppendTag(payload, 1, protowire.VarintType)
		payload = protowire.AppendVarint(payload, last)
		got, err := runlog.Sequence(payload)
		return err == nil && got == last
	}, nil); err != nil {
		t.Fatal(err)
	}
}

func TestSequenceRejectsMalformedEnvelope(t *testing.T) {
	for _, payload := range [][]byte{{0}, {0x08, 0xff}, {0x0a, 0}, {0x12, 10, 1}, {0x80}} {
		_, err := runlog.Sequence(payload)
		var corruption *runlog.Corruption
		if !errors.As(err, &corruption) {
			t.Fatalf("Sequence(%x) = %v", payload, err)
		}
	}
}

func FuzzSequence(f *testing.F) {
	f.Add([]byte{0x08, 0x01})
	f.Add([]byte{0x08, 0x01, 0x12, 0x00})
	f.Fuzz(func(t *testing.T, payload []byte) {
		before := bytes.Clone(payload)
		runlog.Sequence(payload)
		if !bytes.Equal(payload, before) {
			t.Fatal("Sequence changed original bytes")
		}
	})
}
