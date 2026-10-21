package server_test

import (
	"bytes"
	"context"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"

	apiv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/api/v1"
	"github.com/uw-syfi/vibesys/gateway/internal/runlog"
	"github.com/uw-syfi/vibesys/gateway/internal/server"
	"google.golang.org/protobuf/encoding/protowire"
	"google.golang.org/protobuf/proto"
)

type fakeClock struct {
	ticker   *fakeTicker
	interval time.Duration
}
type fakeTicker struct {
	ticks   chan time.Time
	stopped chan struct{}
}

func (c *fakeClock) NewTicker(interval time.Duration) server.Ticker {
	c.interval = interval
	return c.ticker
}
func (t *fakeTicker) Ticks() <-chan time.Time { return t.ticks }
func (t *fakeTicker) Stop()                   { close(t.stopped) }

func TestWatchRunsDetectsBareLockReleaseOnTick(t *testing.T) {
	for _, withHead := range []bool{false, true} {
		t.Run(fmt.Sprint(withHead), func(t *testing.T) {
			ticker := &fakeTicker{ticks: make(chan time.Time, 1), stopped: make(chan struct{})}
			clock := &fakeClock{ticker: ticker}
			f := startWithClock(t, clock)
			lock, err := os.OpenFile(filepath.Join(f.run, "run.lock"), os.O_CREATE|os.O_RDWR, 0600)
			if err != nil {
				t.Fatal(err)
			}
			t.Cleanup(func() { lock.Close() })
			if err := syscall.Flock(int(lock.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
				t.Fatal(err)
			}
			if withHead {
				publishHead(t, f, head(proto.Uint64(0)))
			}
			ctx, cancel := context.WithCancel(f.ctx)
			defer cancel()
			stream, err := client(t, f, false).WatchRuns(ctx, &apiv1.WatchRunsRequest{})
			if err != nil {
				t.Fatal(err)
			}
			if got := watchEntry(t, stream); got.Liveness != apiv1.RunLiveness_RUN_LIVENESS_ALIVE {
				t.Fatal(got)
			}
			if !watchEntry(t, stream).InitialComplete {
				t.Fatal("missing initial marker")
			}
			// Closing the writer fd models process death without a content change.
			if err := lock.Close(); err != nil {
				t.Fatal(err)
			}
			ticker.ticks <- time.Time{}
			got := watchEntry(t, stream)
			if got.RunId != "run-1" || got.Liveness != apiv1.RunLiveness_RUN_LIVENESS_INTERRUPTED ||
				(withHead && got.Head.GetCommittedSeq() != 0) {
				t.Fatal(got)
			}
			cancel()
			select {
			case <-ticker.stopped:
			case <-f.ctx.Done():
				t.Fatal("subscription did not stop its ticker")
			}
			// Stop closes the channel after NewTicker set the interval.
			if clock.interval != server.LivenessProbeInterval {
				t.Fatalf("interval=%v", clock.interval)
			}
		})
	}
}

func TestSkeletonBoundedPrefixContinuesThroughTail(t *testing.T) {
	for _, kind := range []string{"phase1", "committed", "keyframe", "oversized-keyframe", "large-first-record", "zero-and-gaps"} {
		t.Run(kind, func(t *testing.T) {
			f := start(t)
			const count = 12
			records := make([][]byte, count)
			data := []byte(runlog.Magic)
			offsets := []int{len(data)}
			for i := range records {
				seq := uint64(i + 1)
				if kind == "zero-and-gaps" {
					seq = uint64(i * 3)
				}
				size := 128 << 10
				if kind == "large-first-record" && i == 0 {
					// A maximum legal record fits Tail, but its response
					// framing exceeds the unary Skeleton byte budget.
					size = runlog.MaxRecordBytes
					size -= len(payload(t, seq, strings.Repeat("x", size))) - runlog.MaxRecordBytes
				}
				records[i] = payload(t, seq, strings.Repeat("x", size))
				data = append(data, frame(records[i])...)
				offsets = append(offsets, len(data))
			}
			write(t, filepath.Join(f.run, "run.log"), false, data)
			if kind == "committed" || kind == "keyframe" || kind == "oversized-keyframe" {
				publishHead(t, f, head(proto.Uint64(count)))
			}
			startRecord := 0
			var keyframe []byte
			if kind == "keyframe" || kind == "oversized-keyframe" {
				keyframe = publishKeyframe(t, f, 2, offsets[2])
				if kind == "oversized-keyframe" {
					keyframe = protowire.AppendTag(keyframe, 101, protowire.BytesType)
					keyframe = protowire.AppendBytes(keyframe, bytes.Repeat([]byte("x"), 1<<20))
					write(t, filepath.Join(f.run, "keyframes", "2.pb"), false, keyframe)
					keyframe = nil
				} else {
					startRecord = 2
				}
			}
			c := client(t, f, false)
			got, err := c.Skeleton(f.ctx, &apiv1.SkeletonRequest{RunId: "run-1"})
			if err != nil {
				t.Fatal(err)
			}
			if proto.Size(got) > 1<<20 || got.Complete || !bytes.Equal(got.Keyframe, keyframe) {
				t.Fatalf("size=%d complete=%v keyframe=%d", proto.Size(got), got.Complete, len(got.Keyframe))
			}
			consumed := startRecord
			for _, record := range got.Records {
				if consumed >= count || !bytes.Equal(record, records[consumed]) {
					t.Fatalf("prefix record %d differs", consumed)
				}
				consumed++
			}
			if consumed >= count {
				t.Fatal("fixture did not exceed budget")
			}
			next, err := runlog.Sequence(records[consumed])
			if err != nil || got.NextSeq != next {
				t.Fatalf("next_seq=%d want %d, err=%v", got.NextSeq, next, err)
			}
			if kind == "large-first-record" && consumed != 0 {
				t.Fatal("large record included")
			}
			stream, err := c.Tail(f.ctx, &apiv1.TailRequest{RunId: "run-1", FromSeq: got.NextSeq})
			if err != nil {
				t.Fatal(err)
			}
			for consumed < count {
				batch, err := stream.Recv()
				if err != nil {
					t.Fatal(err)
				}
				if len(batch.Records) == 0 {
					t.Fatal("empty Tail batch")
				}
				for _, record := range batch.Records {
					if consumed >= count || !bytes.Equal(record, records[consumed]) {
						t.Fatalf("continuation record %d differs", consumed)
					}
					consumed++
				}
			}
		})
	}
}
