package server_test

import (
	"bytes"
	"os"
	"path/filepath"
	"testing"

	apiv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/api/v1"
	storev1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/store/v1"
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
