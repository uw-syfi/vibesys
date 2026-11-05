package results_test

import (
	"bytes"
	"errors"
	"io"
	"sync"
	"testing"
	"time"

	pb "github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/loadgen/gen/vibesys/evaluator/v1"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/loadgen/internal/files"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/loadgen/internal/results"
)

// gatedWriter blocks every write until it is opened: a results reader that has stopped reading.
type gatedWriter struct {
	open chan struct{}
	mu   sync.Mutex
	buf  bytes.Buffer
}

func (w *gatedWriter) Write(p []byte) (int, error) {
	<-w.open
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.buf.Write(p)
}

func readAll(t *testing.T, data []byte, format files.Format) []uint64 {
	t.Helper()
	reader, _, err := files.NewReader(bytes.NewReader(data), format, pb.FileKind_FILE_KIND_RESULTS)
	if err != nil {
		t.Fatal(err)
	}
	var indexes []uint64
	for {
		result := &pb.OperationResult{}
		if err := reader.Read(result); errors.Is(err, io.EOF) {
			return indexes
		} else if err != nil {
			t.Fatal(err)
		}
		indexes = append(indexes, result.GetIndex())
	}
}

// A results reader that stops reading never blocks Put: results spill to disk, and once the
// reader resumes every result is in the file, in the order Put saw them.
func TestPutNeverWaitsForABlockedReaderAndOrderSurvivesTheSpill(t *testing.T) {
	for _, format := range []files.Format{files.Binpb, files.JSON} {
		out := &gatedWriter{open: make(chan struct{})}
		writer, err := results.New(out, format, results.Options{Queue: 8, SpillDir: t.TempDir(), Writer: "test"})
		if err != nil {
			t.Fatal(err)
		}
		const total = 20_000
		body := bytes.Repeat([]byte("x"), 200) // 4 MB in all: more than every buffer on the way
		done := make(chan struct{})
		go func() {
			defer close(done)
			for i := range uint64(total) {
				writer.Put(&pb.OperationResult{Index: i, Steps: []*pb.StepResult{{Response: &pb.StepResult_Body{Body: body}}}})
			}
		}()
		select {
		case <-done:
		case <-time.After(time.Minute): // hang guard only
			t.Fatal("Put blocked on the results file")
		}
		close(out.open)
		spilled, err := writer.Close()
		if err != nil {
			t.Fatal(err)
		}
		if spilled == 0 {
			t.Fatal("expected results to spill while the reader was blocked")
		}
		indexes := readAll(t, out.buf.Bytes(), format)
		if len(indexes) != total {
			t.Fatalf("got %d results, want %d", len(indexes), total)
		}
		for i, index := range indexes {
			if index != uint64(i) {
				t.Fatalf("result %d has index %d: order lost", i, index)
			}
		}
	}
}

// Concurrent Puts, with the file blocked part of the time, all arrive exactly once.
func TestConcurrentPutsArriveExactlyOnce(t *testing.T) {
	out := &gatedWriter{open: make(chan struct{})}
	writer, err := results.New(out, files.Binpb, results.Options{Queue: 4, SpillDir: t.TempDir(), Writer: "test"})
	if err != nil {
		t.Fatal(err)
	}
	var wg sync.WaitGroup
	const senders, each = 8, 2000
	for s := range uint64(senders) {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for i := range uint64(each) {
				if s == 0 && i == each/2 {
					close(out.open)
				}
				writer.Put(&pb.OperationResult{Index: s*each + i})
			}
		}()
	}
	wg.Wait()
	if _, err := writer.Close(); err != nil {
		t.Fatal(err)
	}
	seen := map[uint64]bool{}
	for _, index := range readAll(t, out.buf.Bytes(), files.Binpb) {
		if seen[index] {
			t.Fatalf("index %d twice", index)
		}
		seen[index] = true
	}
	if len(seen) != senders*each {
		t.Fatalf("got %d results, want %d", len(seen), senders*each)
	}
}

type failingWriter struct{ after int }

func (w *failingWriter) Write(p []byte) (int, error) {
	if w.after <= 0 {
		return 0, errors.New("disk full")
	}
	w.after--
	return len(p), nil
}

func TestCloseReportsAWriteError(t *testing.T) {
	writer, err := results.New(&failingWriter{}, files.Binpb, results.Options{Writer: "test"})
	if err != nil {
		t.Fatal(err)
	}
	for i := range uint64(100_000) {
		writer.Put(&pb.OperationResult{Index: i})
	}
	if _, err := writer.Close(); err == nil {
		t.Fatal("want the write error")
	}
}
