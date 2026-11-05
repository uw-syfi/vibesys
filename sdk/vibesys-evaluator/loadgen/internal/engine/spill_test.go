package engine_test

import (
	"bytes"
	"context"
	"errors"
	"io"
	"net/http"
	"sync"
	"testing"
	"time"

	pb "github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/loadgen/gen/vibesys/evaluator/v1"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/loadgen/internal/engine"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/loadgen/internal/files"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/loadgen/internal/results"
)

// stalledReader is a results file whose reader reads nothing until it is released.
type stalledReader struct {
	release chan struct{}
	mu      sync.Mutex
	buf     bytes.Buffer
}

func (s *stalledReader) Write(p []byte) (int, error) {
	<-s.release
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.buf.Write(p)
}

// A results reader that reads nothing during the phase costs the open loop nothing: results spill
// to disk, the load is sustained, and every result reaches the file once the reader resumes.
func TestPhaseIsSustainedWhileTheResultsReaderIsStalled(t *testing.T) {
	client := serve(t, func(w http.ResponseWriter, r *http.Request) { _, _ = w.Write(bytes.Repeat([]byte("b"), 512)) })
	out := &stalledReader{release: make(chan struct{})}
	sink, err := results.New(out, files.Binpb, results.Options{Queue: 16, SpillDir: t.TempDir(), Writer: "test"})
	if err != nil {
		t.Fatal(err)
	}
	p := indexedPlanKeeping(t, 1200)
	ctx, cancel := context.WithTimeout(context.Background(), time.Minute) // hang guard only
	defer cancel()
	config := engine.Config{
		Load:   engine.Open{Rate: 1000, Arrivals: pb.Arrivals_ARRIVALS_UNIFORM, MaxInFlight: 64, MinSustained: 0.8},
		Window: time.Second, Timeout: 5 * time.Second,
	}
	outcome, err := engine.Run(ctx, config, p, client, sink)
	if err != nil {
		t.Fatal(err)
	}
	close(out.release)
	spilled, err := sink.Close()
	if err != nil {
		t.Fatal(err)
	}
	outcome.Phase.ResultsSpilled = spilled
	s, err := outcome.Aggregator.Summary(outcome.Phase)
	if err != nil {
		t.Fatal(err)
	}
	if !s.GetSustained() || s.GetResultsSpilled() == 0 || s.GetErrors() != 0 {
		t.Fatalf("summary: sustained %v, results spilled %d, errors %d", s.GetSustained(), s.GetResultsSpilled(), s.GetErrors())
	}
	reader, _, err := files.NewReader(bytes.NewReader(out.buf.Bytes()), files.Binpb, pb.FileKind_FILE_KIND_RESULTS)
	if err != nil {
		t.Fatal(err)
	}
	count := 0
	for {
		result := &pb.OperationResult{}
		if err := reader.Read(result); errors.Is(err, io.EOF) {
			break
		} else if err != nil {
			t.Fatal(err)
		}
		if len(result.GetSteps()[0].GetBody()) != 512 {
			t.Fatalf("result %d lost its body", result.GetIndex())
		}
		count++
	}
	if count != 1000 {
		t.Fatalf("%d results in the file, want 1000", count)
	}
}
