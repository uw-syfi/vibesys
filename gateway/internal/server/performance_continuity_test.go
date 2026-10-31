package server_test

import (
	"bytes"
	"compress/gzip"
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	apiv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/api/v1"
	storev1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/store/v1"
	"github.com/uw-syfi/vibesys/gateway/internal/runlog"
	"google.golang.org/protobuf/proto"
	"io"
	"os"
	"path/filepath"
	"testing"
)

// Generated and retained writers, growing prefixes, checkpoints and reconnect cursors all
// publish exactly the same canonical facts through the runs topic, Skeleton and Tail.
func TestEveryLiveReadPathCarriesCanonicalPerformance(t *testing.T) {
	f, err := os.Open("../../../proto/testdata/fold/cases.json.gz")
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	z, err := gzip.NewReader(f)
	if err != nil {
		t.Fatal(err)
	}
	defer z.Close()
	var cases []struct {
		Name string
		Log  string
	}
	if err := json.NewDecoder(z).Decode(&cases); err != nil {
		t.Fatal(err)
	}
	for _, c := range cases {
		t.Run(c.Name, func(t *testing.T) {
			raw, err := base64.StdEncoding.DecodeString(c.Log)
			if err != nil {
				t.Fatal(err)
			}
			reader := runlog.NewReader(bytes.NewReader(raw))
			var payloads [][]byte
			for {
				p, err := reader.Next()
				if err == io.EOF {
					break
				}
				if err != nil {
					t.Fatal(err)
				}
				payloads = append(payloads, p)
			}
			f := start(t)
			c := client(t, f, false)
			projection := runlog.NewProjection()
			data := []byte(runlog.Magic)
			next := 0
			cursor := uint64(1)
			for stage, through := range []int{len(payloads) / 2, len(payloads) - 1, len(payloads)} {
				for next < through {
					p := payloads[next]
					data = append(data, frame(p)...)
					if err := projection.Record(p); err != nil {
						t.Fatal(err)
					}
					next++
				}
				want, err := runlog.ProjectHead(projection.State)
				if err != nil || want == nil {
					t.Fatal("missing generated declaration", err)
				}
				write(t, filepath.Join(f.run, "run.log"), false, data)
				prior := proto.Clone(want).(*storev1.RunHead)
				prior.PerformanceStated = false
				prior.Performance = nil
				publishHead(t, f, prior)
				if stage == 1 {
					state := proto.Clone(projection.State).(*storev1.Keyframe)
					state.LogOffset = uint64(len(data))
					if err := os.MkdirAll(filepath.Join(f.run, "keyframes"), 0700); err != nil {
						t.Fatal(err)
					}
					write(t, filepath.Join(f.run, "keyframes", fmt.Sprintf("%d.pb", want.GetCommittedSeq())), false, encode(t, state))
				}
				watchCtx, cancel := context.WithCancel(f.ctx)
				watch, err := watchRuns(watchCtx, c)
				if err != nil {
					t.Fatal(err)
				}
				observed := watchEntry(t, watch)
				assertSamePerformance(t, observed.Head, want)
				cancel()
				skeleton, err := c.Skeleton(f.ctx, &apiv1.SkeletonRequest{RunId: "run-1"})
				if err != nil {
					t.Fatal(err)
				}
				assertSamePerformance(t, skeleton.Head, want)
				tailCtx, stop := context.WithCancel(f.ctx)
				tail, err := c.Tail(tailCtx, &apiv1.TailRequest{RunId: "run-1", FromSeq: cursor})
				if err != nil {
					t.Fatal(err)
				}
				for {
					batch, err := tail.Recv()
					if err != nil {
						t.Fatal(err)
					}
					if batch.Head == nil || !batch.Head.PerformanceStated || batch.Head.GetCommittedSeq() != batch.ThroughSeq {
						t.Fatalf("Tail lost exact-prefix facts: %v", batch.Head)
					}
					if batch.ThroughSeq == want.GetCommittedSeq() {
						assertSamePerformance(t, batch.Head, want)
						break
					}
				}
				stop()
				cursor = want.GetCommittedSeq() + 1
			}
		})
	}
}
func assertSamePerformance(t *testing.T, got, want *storev1.RunHead) {
	t.Helper()
	if got == nil || !got.PerformanceStated || got.GetCommittedSeq() != want.GetCommittedSeq() {
		t.Fatalf("missing committed performance: %v", got)
	}
	if len(got.Performance) != len(want.Performance) {
		t.Fatal("performance series differ")
	}
	for i, p := range want.Performance {
		if !proto.Equal(got.Performance[i], p) {
			t.Fatalf("facts differ at series %d", i)
		}
	}
}
