package server_test

import (
	"bytes"
	"fmt"
	"os"
	"path/filepath"
	"syscall"
	"testing"

	apiv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/api/v1"
	storev1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/store/v1"
	"github.com/uw-syfi/vibesys/gateway/internal/runlog"
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
		{"awaiting-with-lifecycle", func(t *testing.T, f fixture) {
			value := head(proto.Uint64(0))
			value.Epoch = 5
			value.AwaitingLifecycle = true
			publishHead(t, f, value)
		}, codes.DataLoss},
		{"awaiting-without-epoch", func(t *testing.T, f fixture) {
			value := head(proto.Uint64(0))
			value.Lifecycle = nil
			value.AwaitingLifecycle = true
			publishHead(t, f, value)
		}, codes.DataLoss},
		{"awaiting-without-commit", func(t *testing.T, f fixture) {
			value := head(nil)
			value.Epoch = 5
			value.Lifecycle = nil
			value.AwaitingLifecycle = true
			publishHead(t, f, value)
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

func TestRunsIndexCreatedAfterSubscription(t *testing.T) {
	f := start(t)
	if err := os.Remove(filepath.Join(f.root, "runs", "run-1")); err != nil {
		t.Fatal(err)
	}
	if err := os.Remove(filepath.Join(f.root, "runs")); err != nil {
		t.Fatal(err)
	}
	stream, err := watchRuns(f.ctx, client(t, f, false))
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

func TestRunsDoesNotMistakeAnotherProbeForWriter(t *testing.T) {
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
	stream, err := watchRuns(f.ctx, client(t, f, false))
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

func TestRunsReadsAnEpochAwaitingItsFirstLifecycle(t *testing.T) {
	f := start(t)
	pending := head(proto.Uint64(0))
	pending.Epoch = 5
	pending.Lifecycle = nil
	pending.AwaitingLifecycle = true
	publishHead(t, f, pending)
	c := client(t, f, false)
	stream, err := watchRuns(f.ctx, c)
	if err != nil {
		t.Fatal(err)
	}
	got := watchEntry(t, stream)
	if got.Head.GetEpoch() != 5 || !got.Head.GetAwaitingLifecycle() || got.Head.GetLifecycle() != nil {
		t.Fatalf("pending epoch = %v", got)
	}
	if _, err := c.Skeleton(f.ctx, &apiv1.SkeletonRequest{RunId: "run-1"}); err != nil {
		t.Fatalf("read epoch before lifecycle: %v", err)
	}
}

func TestRunsParkedEpochIsReadableAfterWriterReleasesLock(t *testing.T) {
	for _, lockExists := range []bool{false, true} {
		t.Run(fmt.Sprintf("lock-file-%t", lockExists), func(t *testing.T) {
			f := start(t)
			if lockExists {
				lock, err := os.OpenFile(filepath.Join(f.run, "run.lock"), os.O_CREATE|os.O_RDWR, 0600)
				if err != nil {
					t.Fatal(err)
				}
				if err := lock.Close(); err != nil {
					t.Fatal(err)
				}
			}
			parked := head(proto.Uint64(0))
			parked.Lifecycle = &storev1.Lifecycle{State: &storev1.Lifecycle_Parked{Parked: &storev1.Parked{}}}
			publishHead(t, f, parked)
			c := client(t, f, false)
			stream, err := watchRuns(f.ctx, c)
			if err != nil {
				t.Fatal(err)
			}
			got := watchEntry(t, stream)
			if got.Liveness != apiv1.RunLiveness_RUN_LIVENESS_PARKED || got.Head.GetLifecycle().GetParked() == nil {
				t.Fatalf("parked epoch = %v", got)
			}
			if _, err := c.Skeleton(f.ctx, &apiv1.SkeletonRequest{RunId: "run-1"}); err != nil {
				t.Fatalf("read parked epoch: %v", err)
			}
		})
	}
}
