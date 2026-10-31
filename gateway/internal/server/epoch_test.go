package server_test

import (
	"bytes"
	"os"
	"path/filepath"
	"testing"

	storev1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/store/v1"
	"github.com/uw-syfi/vibesys/gateway/internal/runlog"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
)

// Retained phase-1 heads lack a canonical epoch. Informational process epochs
// may disagree, and a complete successor beyond committed_seq is unpublished.
func TestWatchRunsDecodesPhase1EpochFromPublishedLog(t *testing.T) {
	for _, committed := range []*uint64{nil, proto.Uint64(0), proto.Uint64(5)} {
		for _, process := range []*storev1.RunProcess{nil, {Epoch: 99, Pid: 1, StartedAt: &timestamppb.Timestamp{Seconds: 1}}} {
			t.Run("published-prefix", func(t *testing.T) {
				f := start(t)
				data := []byte(runlog.Magic)
				for _, entry := range []struct {
					seq   uint64
					epoch uint32
				}{{0, 3}, {5, 7}, {8, 11}} {
					data = append(data, frame(encode(t, &storev1.Record{Seq: entry.seq, Epoch: 53,
						Body: &storev1.Record_EpochStarted{EpochStarted: &storev1.EpochStarted{Epoch: entry.epoch}},
					}))...)
				}
				write(t, filepath.Join(f.run, "run.log"), false, data)
				original := head(committed)
				original.Process = process
				publishHead(t, f, original)
				originalBytes := encode(t, original)
				stream, err := watchRuns(f.ctx, client(t, f, false))
				if err != nil {
					t.Fatal(err)
				}
				got := watchEntry(t, stream)
				want := uint32(11)
				if committed != nil {
					want = 7
					if *committed == 0 {
						want = 0
					}
				}
				if got.Head.GetEpoch() != want || !proto.Equal(got.Head.GetProcess(), process) {
					t.Fatalf("epoch = %d, want %d; process = %v", got.Head.GetEpoch(), want, got.Head.GetProcess())
				}
				for name, before := range map[string][]byte{"run.head": originalBytes, "run.log": data} {
					after, err := os.ReadFile(filepath.Join(f.run, name))
					if err != nil || !bytes.Equal(after, before) {
						t.Fatalf("decoder rewrote %s: %v", name, err)
					}
				}
			})
		}
	}
}
