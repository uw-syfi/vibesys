package runlog

import (
	"bytes"
	"testing"
	"testing/quick"

	storev1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/store/v1"
	"google.golang.org/protobuf/encoding/protowire"
	"google.golang.org/protobuf/proto"
)

func epochPayload(seq uint64, epoch uint32) []byte {
	payload, err := proto.Marshal(&storev1.Record{Seq: seq, Epoch: 99,
		Body: &storev1.Record_EpochStarted{EpochStarted: &storev1.EpochStarted{Epoch: epoch}},
	})
	if err != nil {
		panic(err)
	}
	return payload
}

func TestCurrentEpochNumberUsesOnlyPublishedDeclarations(t *testing.T) {
	property := func(epoch uint32) bool {
		if epoch == 0 {
			epoch = 1
		}
		data := append([]byte(Magic), framed(epochPayload(0, epoch))...)
		// Gaps, zero-based sequences and repeated declarations are retained formats.
		data = append(data, framed(epochPayload(5, epoch))...)
		data = append(data, framed(epochPayload(8, ^uint32(0)))...)
		before := bytes.Clone(data)
		for _, limit := range []uint64{0, 5, 8} {
			got, err := CurrentEpochNumber(bytes.NewReader(data), &limit)
			want := epoch
			if limit == 0 {
				want = 0
			}
			if limit == 8 {
				want = ^uint32(0)
			}
			if err != nil || got != want {
				return false
			}
		}
		got, err := CurrentEpochNumber(bytes.NewReader(data), nil)
		return err == nil && got == ^uint32(0) && bytes.Equal(data, before)
	}
	if err := quick.Check(property, nil); err != nil {
		t.Fatal(err)
	}
}

func TestCurrentEpochNumberLeavesOtherBodiesOpaque(t *testing.T) {
	fields := (&storev1.Record{}).ProtoReflect().Descriptor().Fields()
	span := fields.ByName("span").Number()
	payload := protowire.AppendTag(epochPayload(1, 7), span, protowire.BytesType)
	// Invalid as a Span protobuf, but valid opaque bytes for the gateway. Its
	// oneof selection supersedes the earlier epoch_started in this same record.
	payload = protowire.AppendBytes(payload, []byte{0xff})
	data := append([]byte(Magic), framed(payload)...)
	got, err := CurrentEpochNumber(bytes.NewReader(data), nil)
	if err != nil || got != 0 {
		t.Fatalf("epoch = %d, error = %v", got, err)
	}
	// A later declaration supersedes the opaque body without parsing that body.
	payload = append(payload, epochPayload(1, 9)...)
	data = append([]byte(Magic), framed(payload)...)
	got, err = CurrentEpochNumber(bytes.NewReader(data), nil)
	if err != nil || got != 9 {
		t.Fatalf("epoch = %d, error = %v", got, err)
	}
}

func TestCurrentEpochNumberRejectsUnpublishedOrCorruptPrefixes(t *testing.T) {
	good := append([]byte(Magic), framed(epochPayload(1, 7))...)
	corrupt := append(bytes.Clone(good), framed(epochPayload(2, 9))...)
	corrupt[len(corrupt)-1] ^= 1
	for name, data := range map[string][]byte{
		"missing committed record": good,
		"torn committed record":    append(bytes.Clone(good), 0x10),
		"corrupt committed record": corrupt,
		"decreasing sequence":      append(bytes.Clone(good), framed(epochPayload(0, 9))...),
	} {
		t.Run(name, func(t *testing.T) {
			limit := uint64(2)
			if _, err := CurrentEpochNumber(bytes.NewReader(data), &limit); err == nil {
				t.Fatal("invalid committed prefix accepted")
			}
			limit = 1
			got, err := CurrentEpochNumber(bytes.NewReader(data), &limit)
			if err != nil || got != 7 {
				t.Fatalf("published prefix rejected: %d, %v", got, err)
			}
		})
	}
	torn := append(bytes.Clone(good), 0x10)
	got, err := CurrentEpochNumber(bytes.NewReader(torn), nil)
	if err != nil || got != 7 {
		t.Fatalf("legacy complete prefix rejected: %d, %v", got, err)
	}
}
