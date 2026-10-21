package server_test

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"

	apiv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/api/v1"
	storev1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/store/v1"
	"github.com/uw-syfi/vibesys/gateway/internal/runlog"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

func TestSkeletonRejectsBrokenCommitAndKeyframe(t *testing.T) {
	for _, tc := range []struct {
		name  string
		setup func(*testing.T, fixture)
		code  codes.Code
	}{
		{"corrupt-head", func(t *testing.T, f fixture) { write(t, filepath.Join(f.run, "run.head"), false, []byte{0xff}) }, codes.DataLoss},
		{"missing-lifecycle", func(t *testing.T, f fixture) {
			write(t, filepath.Join(f.run, "run.head"), false, encode(t, &storev1.RunHead{}))
		}, codes.DataLoss},
		{"commit-past-tail", func(t *testing.T, f fixture) { publishHead(t, f, head(proto.Uint64(2))) }, codes.DataLoss},
		{"keyframe-mismatched-sequence", func(t *testing.T, f fixture) {
			publishKeyframe(t, f, 1, 8)
			write(t, filepath.Join(f.run, "keyframes", "1.pb"), false, encode(t, &storev1.Keyframe{ThroughSeq: 2, LogOffset: 8}))
		}, codes.DataLoss},
		{"keyframe-offset-overflow", func(t *testing.T, f fixture) {
			publishKeyframe(t, f, 1, 8)
			write(t, filepath.Join(f.run, "keyframes", "1.pb"), false, encode(t, &storev1.Keyframe{ThroughSeq: 1, LogOffset: ^uint64(0)}))
		}, codes.DataLoss},
		{"keyframe-offset-past-log", func(t *testing.T, f fixture) { publishKeyframe(t, f, 1, 100000) }, codes.DataLoss},
		{"keyframe-truncated-header-tail", func(t *testing.T, f fixture) {
			data := publishKeyframe(t, f, 1, 8)
			write(t, filepath.Join(f.run, "keyframes", "1.pb"), false, append(data, 0x08))
		}, codes.DataLoss},
		{"keyframe-bad-header", func(t *testing.T, f fixture) {
			publishKeyframe(t, f, 1, 8)
			write(t, filepath.Join(f.run, "keyframes", "1.pb"), false, []byte{0x0a, 0})
		}, codes.DataLoss},
	} {
		t.Run(tc.name, func(t *testing.T) {
			f := start(t)
			writeFrames(t, f, 1)
			tc.setup(t, f)
			_, err := client(t, f, false).Skeleton(f.ctx, &apiv1.SkeletonRequest{RunId: "run-1"})
			if status.Code(err) != tc.code {
				t.Fatalf("Skeleton=%v, want %v", err, tc.code)
			}
		})
	}
}

func TestSkeletonDoesNotReadUncommittedCorruption(t *testing.T) {
	f := start(t)
	data, offsets := writeFrames(t, f, 2)
	data[offsets[2]-1] ^= 1
	write(t, filepath.Join(f.run, "run.log"), false, data)
	publishHead(t, f, head(proto.Uint64(1)))
	got, err := client(t, f, false).Skeleton(f.ctx, &apiv1.SkeletonRequest{RunId: "run-1"})
	if err != nil || got.GetCommittedSeq() != 1 || len(got.GetRecords()) != 1 {
		t.Fatalf("Skeleton=%v,%v", got, err)
	}
	if !bytes.Equal(got.Records[0], payload(t, 1, "1")) {
		t.Fatal(got)
	}
}

func TestWatchRunsIndexCreatedAfterSubscription(t *testing.T) {
	f := start(t)
	if err := os.Remove(filepath.Join(f.root, "runs", "run-1")); err != nil {
		t.Fatal(err)
	}
	if err := os.Remove(filepath.Join(f.root, "runs")); err != nil {
		t.Fatal(err)
	}
	stream, err := client(t, f, false).WatchRuns(f.ctx, &apiv1.WatchRunsRequest{})
	if err != nil {
		t.Fatal(err)
	}
	if got := watchEntry(t, stream); !got.InitialComplete {
		t.Fatal(got)
	}
	if err := os.Mkdir(filepath.Join(f.root, "runs"), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(f.run, filepath.Join(f.root, "runs", "run-1")); err != nil {
		t.Fatal(err)
	}
	got := watchEntry(t, stream)
	if got.RunId != "run-1" || got.Head != nil || got.Liveness != apiv1.RunLiveness_RUN_LIVENESS_UNSPECIFIED {
		t.Fatal(got)
	}
	publishHead(t, f, head(proto.Uint64(0)))
	if got := watchEntry(t, stream); got.Head == nil || got.Liveness != apiv1.RunLiveness_RUN_LIVENESS_INTERRUPTED {
		t.Fatal(got)
	}
}

func TestSkeletonBeforeLogCreation(t *testing.T) {
	for _, committed := range []*uint64{nil, proto.Uint64(0), proto.Uint64(1)} {
		f := start(t)
		if committed != nil {
			publishHead(t, f, head(committed))
		}
		got, err := client(t, f, false).Skeleton(f.ctx, &apiv1.SkeletonRequest{RunId: "run-1"})
		if committed != nil && *committed > 0 {
			if status.Code(err) != codes.DataLoss {
				t.Fatalf("missing committed log = %v", err)
			}
		} else if err != nil || got.GetCommittedSeq() != 0 || len(got.GetRecords()) != 0 || len(got.GetKeyframe()) != 0 {
			t.Fatalf("empty Skeleton = %v,%v", got, err)
		}
	}
}

func TestWatchRunsDoesNotMistakeAnotherProbeForWriter(t *testing.T) {
	f := start(t)
	lock, err := os.OpenFile(filepath.Join(f.run, "run.lock"), os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		t.Fatal(err)
	}
	defer lock.Close()
	// A shared lock models a concurrent gateway probe; the writer uses LOCK_EX.
	if err := syscall.Flock(int(lock.Fd()), syscall.LOCK_SH|syscall.LOCK_NB); err != nil {
		t.Fatal(err)
	}
	publishHead(t, f, head(proto.Uint64(0)))
	stream, err := client(t, f, false).WatchRuns(f.ctx, &apiv1.WatchRunsRequest{})
	if err != nil {
		t.Fatal(err)
	}
	if got := watchEntry(t, stream); got.Liveness != apiv1.RunLiveness_RUN_LIVENESS_INTERRUPTED {
		t.Fatal(got)
	}
}

func TestPhase1SequenceZeroRemainsReadable(t *testing.T) {
	f := start(t)
	zero, one := payload(t, 0, "zero"), payload(t, 1, "one")
	data := append([]byte(runlog.Magic), frame(zero)...)
	data = append(data, frame(one)...)
	write(t, filepath.Join(f.run, "run.log"), false, data)
	c := client(t, f, false)
	stream, err := c.Tail(f.ctx, &apiv1.TailRequest{RunId: "run-1"})
	if err != nil {
		t.Fatal(err)
	}
	got, err := stream.Recv()
	if err != nil || len(got.GetRecords()) != 2 || !bytes.Equal(got.Records[0], zero) || !bytes.Equal(got.Records[1], one) {
		t.Fatalf("phase 1 Tail = %v,%v", got, err)
	}
	skeleton, err := c.Skeleton(f.ctx, &apiv1.SkeletonRequest{RunId: "run-1"})
	if err != nil || len(skeleton.GetRecords()) != 2 || !bytes.Equal(skeleton.Records[0], zero) || !bytes.Equal(skeleton.Records[1], one) {
		t.Fatalf("phase 1 Skeleton = %v,%v", skeleton, err)
	}
}

func TestSkeletonPhase1HasNoSmallHistoryCap(t *testing.T) {
	f := start(t)
	path := filepath.Join(f.run, "run.log")
	write(t, path, false, []byte(runlog.Magic))
	// Phase 1 permits these record sizes. Aggregate history must not acquire an
	// extra 64 MiB cap merely because Skeleton was added alongside Tail.
	text := strings.Repeat("x", 4<<20)
	const count = 17
	for seq := uint64(1); seq <= count; seq++ {
		write(t, path, true, frame(payload(t, seq, text)))
	}
	got, err := client(t, f, false).Skeleton(f.ctx, &apiv1.SkeletonRequest{RunId: "run-1"}, grpc.MaxCallRecvMsgSize(128<<20))
	if err != nil {
		t.Fatal(err)
	}
	if len(got.Keyframe) != 0 || len(got.Records) != count || got.CommittedSeq != count {
		t.Fatalf("Skeleton count=%d, committed=%d", len(got.Records), got.CommittedSeq)
	}
	total := 0
	for i, record := range got.Records {
		seq, err := runlog.Sequence(record)
		if err != nil || seq != uint64(i+1) {
			t.Fatalf("record %d: seq=%d, err=%v", i, seq, err)
		}
		total += len(record)
	}
	if total <= 64<<20 {
		t.Fatal("fixture did not exceed the old history cap")
	}
}
