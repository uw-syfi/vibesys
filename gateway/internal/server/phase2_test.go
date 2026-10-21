package server_test

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
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
	"google.golang.org/protobuf/encoding/protowire"
	"google.golang.org/protobuf/proto"
)

func encode(t *testing.T, value proto.Message) []byte {
	t.Helper()
	data, err := proto.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	return data
}

func head(seq *uint64) *storev1.RunHead {
	return &storev1.RunHead{Lifecycle: &storev1.Lifecycle{State: &storev1.Lifecycle_Running{Running: &storev1.Running{}}}, Strategy: "example", CommittedSeq: seq}
}

func publishHead(t *testing.T, f fixture, value *storev1.RunHead) {
	t.Helper()
	write(t, filepath.Join(f.run, "head.tmp"), false, encode(t, value))
	if err := os.Rename(filepath.Join(f.run, "head.tmp"), filepath.Join(f.run, "run.head")); err != nil {
		t.Fatal(err)
	}
}

func writeFrames(t *testing.T, f fixture, count int) ([]byte, []int) {
	t.Helper()
	data := []byte(runlog.Magic)
	offsets := []int{len(data)}
	for seq := 1; seq <= count; seq++ {
		data = append(data, frame(payload(t, uint64(seq), fmt.Sprint(seq)))...)
		offsets = append(offsets, len(data))
	}
	write(t, filepath.Join(f.run, "run.log"), false, data)
	return data, offsets
}

func publishKeyframe(t *testing.T, f fixture, seq uint64, offset int) []byte {
	t.Helper()
	if err := os.MkdirAll(filepath.Join(f.run, "keyframes"), 0700); err != nil {
		t.Fatal(err)
	}
	data := encode(t, &storev1.Keyframe{ThroughSeq: seq, LogOffset: uint64(offset)})
	// Unknown future skeleton fields pass through exactly, without a gateway fold.
	data = protowire.AppendTag(data, 100, protowire.BytesType)
	data = protowire.AppendBytes(data, []byte("future skeleton"))
	write(t, filepath.Join(f.run, "keyframes", fmt.Sprintf("%d.pb", seq)), false, data)
	return data
}

func TestSkeletonCommittedAndPhase1(t *testing.T) {
	for _, tc := range []struct {
		name      string
		committed *uint64
		keyframe  bool
		want      uint64
	}{
		{name: "phase1", want: 4},
		{name: "head-without-boundary", committed: nil, keyframe: true, want: 4},
		{name: "zero", committed: proto.Uint64(0), want: 0},
		{name: "committed", committed: proto.Uint64(3), keyframe: true, want: 3},
	} {
		t.Run(tc.name, func(t *testing.T) {
			f := start(t)
			_, offsets := writeFrames(t, f, 4)
			if tc.name != "phase1" {
				publishHead(t, f, head(tc.committed))
			}
			var keyframe []byte
			startSeq := uint64(1)
			if tc.keyframe {
				keyframe = publishKeyframe(t, f, 2, offsets[2])
				startSeq = 3
				publishKeyframe(t, f, 4, offsets[4])
			}
			if tc.want == 4 && tc.keyframe {
				keyframe = publishKeyframe(t, f, 4, offsets[4])
				startSeq = 5
			}
			got, err := client(t, f, false).Skeleton(f.ctx, &apiv1.SkeletonRequest{RunId: "run-1"})
			if err != nil {
				t.Fatal(err)
			}
			if got.CommittedSeq != tc.want || !got.Complete || (tc.want > 0 && got.NextSeq != tc.want+1) || !bytes.Equal(got.Keyframe, keyframe) {
				t.Fatalf("Skeleton = %v", got)
			}
			count := 0
			for seq := startSeq; seq <= tc.want; seq++ {
				if count >= len(got.Records) || !bytes.Equal(got.Records[count], payload(t, seq, fmt.Sprint(seq))) {
					t.Fatalf("record %d = %v", seq, got)
				}
				count++
			}
			if len(got.Records) != count {
				t.Fatalf("got %d records, want %d", len(got.Records), count)
			}
		})
	}
}

func TestTailWaitsForCommitAndSeeksKeyframe(t *testing.T) {
	f := start(t)
	data, offsets := writeFrames(t, f, 5)
	publishKeyframe(t, f, 3, offsets[3])
	// A seek from 4 must not decode or checksum the prefix covered by the keyframe.
	data[offsets[1]-1] ^= 1
	write(t, filepath.Join(f.run, "run.log"), false, data)
	publishHead(t, f, head(proto.Uint64(4)))
	c := client(t, f, false)
	stream, err := c.Tail(f.ctx, &apiv1.TailRequest{RunId: "run-1", FromSeq: 4})
	if err != nil {
		t.Fatal(err)
	}
	receive(t, stream, payload(t, 4, "4"), 4)
	// Record 5 was already appended but cannot be exposed before this rename.
	publishHead(t, f, head(proto.Uint64(5)))
	receive(t, stream, payload(t, 5, "5"), 5)
}

func TestTailZeroCommitDoesNotReadUncommittedCorruption(t *testing.T) {
	f := start(t)
	publishHead(t, f, head(proto.Uint64(0)))
	write(t, filepath.Join(f.run, "run.log"), false, []byte("bad tail"))
	c := client(t, f, false)
	ctx, cancel := context.WithCancel(f.ctx)
	defer cancel()
	stream, err := c.Tail(ctx, &apiv1.TailRequest{RunId: "run-1"})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := stream.Header(); err != nil {
		t.Fatal(err)
	}
	// A commit release, not a timeout, synchronizes this test with the reader.
	one := payload(t, 1, "one")
	write(t, filepath.Join(f.run, "run.log"), false, append([]byte(runlog.Magic), frame(one)...))
	publishHead(t, f, head(proto.Uint64(1)))
	receive(t, stream, one, 1)
}

func TestGetBlobsHashesAndPerHashErrors(t *testing.T) {
	f := start(t)
	if err := os.Mkdir(filepath.Join(f.run, "blobs"), 0700); err != nil {
		t.Fatal(err)
	}
	content := []byte("immutable content")
	sum := sha256.Sum256(content)
	hash := hex.EncodeToString(sum[:])
	write(t, filepath.Join(f.run, "blobs", hash), false, content)
	mismatch := strings.Repeat("1", 64)
	write(t, filepath.Join(f.run, "blobs", mismatch), false, []byte("bad"))
	escape := strings.Repeat("2", 64)
	private := filepath.Join(t.TempDir(), "content")
	write(t, private, false, []byte("outside"))
	if err := os.Symlink(private, filepath.Join(f.run, "blobs", escape)); err != nil {
		t.Fatal(err)
	}
	hashes := []string{hash, mismatch, strings.Repeat("0", 64), escape, hash}
	got, err := client(t, f, false).GetBlobs(f.ctx, &apiv1.GetBlobsRequest{RunId: "run-1", Sha256: hashes})
	if err != nil {
		t.Fatal(err)
	}
	if len(got.Blobs) != len(hashes) {
		t.Fatal(got)
	}
	want := []codes.Code{codes.OK, codes.DataLoss, codes.NotFound, codes.Internal, codes.OK}
	for i, result := range got.Blobs {
		if result.Sha256 != hashes[i] || codes.Code(result.ErrorCode) != want[i] {
			t.Fatalf("blob %d = %v", i, result)
		}
		if want[i] == codes.OK && !bytes.Equal(result.Data, content) {
			t.Fatal(result)
		}
		if want[i] != codes.OK && len(result.Data) != 0 {
			t.Fatal(result)
		}
	}
}

func TestGetBlobsResponseBudget(t *testing.T) {
	f := start(t)
	if err := os.Mkdir(filepath.Join(f.run, "blobs"), 0700); err != nil {
		t.Fatal(err)
	}
	content := bytes.Repeat([]byte("x"), 16<<20)
	sum := sha256.Sum256(content)
	hash := hex.EncodeToString(sum[:])
	write(t, filepath.Join(f.run, "blobs", hash), false, content)
	got, err := client(t, f, false).GetBlobs(f.ctx, &apiv1.GetBlobsRequest{RunId: "run-1", Sha256: []string{hash, hash}}, grpc.MaxCallRecvMsgSize(20<<20))
	if err != nil {
		t.Fatal(err)
	}
	if len(got.Blobs) != 2 || len(got.Blobs[0].Data) != len(content) || codes.Code(got.Blobs[1].ErrorCode) != codes.ResourceExhausted {
		t.Fatal("blob response budget not enforced")
	}
}

func TestTailStreamExistingAndLive(t *testing.T) {
	for _, createdLater := range []bool{false, true} {
		t.Run(fmt.Sprint(createdLater), func(t *testing.T) {
			f := start(t)
			path := filepath.Join(f.run, "streams", "output")
			if !createdLater {
				if err := os.Mkdir(filepath.Dir(path), 0700); err != nil {
					t.Fatal(err)
				}
				write(t, path, false, []byte("prefix-first"))
			}
			stream, err := client(t, f, false).TailStream(f.ctx, &apiv1.TailStreamRequest{RunId: "run-1", StreamId: "output", FromOffset: 7})
			if err != nil {
				t.Fatal(err)
			}
			if _, err := stream.Header(); err != nil {
				t.Fatal(err)
			}
			if createdLater {
				if err := os.Mkdir(filepath.Dir(path), 0700); err != nil {
					t.Fatal(err)
				}
				write(t, path, false, []byte("prefix-first"))
			}
			got, err := stream.Recv()
			if err != nil || string(got.GetData()) != "first" || got.GetNextOffset() != 12 {
				t.Fatalf("stream=%v,%v", got, err)
			}
			write(t, path, true, []byte("-live"))
			got, err = stream.Recv()
			if err != nil || string(got.GetData()) != "-live" || got.GetNextOffset() != 17 {
				t.Fatalf("live=%v,%v", got, err)
			}
		})
	}
}

func TestTailStreamRejectsEscapes(t *testing.T) {
	for _, id := range []string{"../run.head", "/absolute", "a/b", "..", ""} {
		t.Run(id, func(t *testing.T) {
			f := start(t)
			stream, err := client(t, f, false).TailStream(f.ctx, &apiv1.TailStreamRequest{RunId: "run-1", StreamId: id})
			if err == nil {
				_, err = stream.Recv()
			}
			if status.Code(err) != codes.InvalidArgument {
				t.Fatalf("escape=%v", err)
			}
		})
	}
}

func watchEntry(t *testing.T, stream apiv1.HostService_WatchRunsClient) *apiv1.WatchRunsResponse {
	t.Helper()
	got, err := stream.Recv()
	if err != nil {
		t.Fatal(err)
	}
	return got
}

func TestWatchRunsAtomicHeadAndRealLock(t *testing.T) {
	f := start(t)
	lock, err := os.OpenFile(filepath.Join(f.run, "run.lock"), os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		t.Fatal(err)
	}
	defer lock.Close()
	if err := syscall.Flock(int(lock.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		t.Fatal(err)
	}
	publishHead(t, f, head(proto.Uint64(1)))
	stream, err := client(t, f, false).WatchRuns(f.ctx, &apiv1.WatchRunsRequest{})
	if err != nil {
		t.Fatal(err)
	}
	first := watchEntry(t, stream)
	if first.RunId != "run-1" || first.Head.GetCommittedSeq() != 1 || first.Liveness != apiv1.RunLiveness_RUN_LIVENESS_ALIVE {
		t.Fatal(first)
	}
	if !watchEntry(t, stream).InitialComplete {
		t.Fatal("missing initial marker")
	}
	publishHead(t, f, head(proto.Uint64(2)))
	if got := watchEntry(t, stream); got.Head.GetCommittedSeq() != 2 || got.Liveness != apiv1.RunLiveness_RUN_LIVENESS_ALIVE {
		t.Fatal(got)
	}
	if err := syscall.Flock(int(lock.Fd()), syscall.LOCK_UN); err != nil {
		t.Fatal(err)
	}
	publishHead(t, f, head(proto.Uint64(3)))
	for {
		got := watchEntry(t, stream)
		// Filesystem notifications can expose the unlock before the next head rename.
		if got.Head.GetCommittedSeq() == 2 && got.Liveness == apiv1.RunLiveness_RUN_LIVENESS_INTERRUPTED {
			continue
		}
		if got.Head.GetCommittedSeq() != 3 || got.Liveness != apiv1.RunLiveness_RUN_LIVENESS_INTERRUPTED {
			t.Fatal(got)
		}
		break
	}
	ended := head(proto.Uint64(4))
	ended.Lifecycle = &storev1.Lifecycle{State: &storev1.Lifecycle_Ended{Ended: &storev1.Ended{Outcome: storev1.Outcome_OUTCOME_FINISHED}}}
	publishHead(t, f, ended)
	if got := watchEntry(t, stream); got.Liveness != apiv1.RunLiveness_RUN_LIVENESS_ENDED {
		t.Fatal(got)
	}
	if err := os.Remove(filepath.Join(f.root, "runs", "run-1")); err != nil {
		t.Fatal(err)
	}
	if got := watchEntry(t, stream); !got.Removed || got.RunId != "run-1" {
		t.Fatal(got)
	}
	if err := os.Symlink(f.run, filepath.Join(f.root, "runs", "run-2")); err != nil {
		t.Fatal(err)
	}
	if got := watchEntry(t, stream); got.RunId != "run-2" || got.Head.GetCommittedSeq() != 4 {
		t.Fatal(got)
	}
}
