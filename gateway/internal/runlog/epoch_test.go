package runlog

import (
	"bytes"
	"testing"

	storev1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/store/v1"
	"google.golang.org/protobuf/proto"
)

func recordPayload(seq uint64) []byte {
	payload, err := proto.Marshal(&storev1.Record{Seq: seq})
	if err != nil {
		panic(err)
	}
	return payload
}

// This fence behavior used to be exercised only through CurrentEpochNumber, a published-prefix
// epoch decoder with no production caller (removed; see git history). ReadPrefix itself is the
// production entry point epoch and input-upgrade decoding both build on.
func TestReadPrefixRejectsUnpublishedOrCorruptCommittedFences(t *testing.T) {
	good := append([]byte(Magic), framed(recordPayload(1))...)
	corrupt := append(bytes.Clone(good), framed(recordPayload(2))...)
	corrupt[len(corrupt)-1] ^= 1
	visit := func([]byte) error { return nil }
	for name, data := range map[string][]byte{
		"missing committed record": good,
		"torn committed record":    append(bytes.Clone(good), 0x10),
		"corrupt committed record": corrupt,
		"decreasing sequence":      append(bytes.Clone(good), framed(recordPayload(0))...),
	} {
		t.Run(name, func(t *testing.T) {
			limit := uint64(2)
			if err := ReadPrefix(bytes.NewReader(data), &limit, visit); err == nil {
				t.Fatal("invalid committed prefix accepted")
			}
			limit = 1
			if err := ReadPrefix(bytes.NewReader(data), &limit, visit); err != nil {
				t.Fatalf("published prefix rejected: %v", err)
			}
		})
	}
	torn := append(bytes.Clone(good), 0x10)
	if err := ReadPrefix(bytes.NewReader(torn), nil, visit); err != nil {
		t.Fatalf("legacy complete prefix rejected: %v", err)
	}
}
