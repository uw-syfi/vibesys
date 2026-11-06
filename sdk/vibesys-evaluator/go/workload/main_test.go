package workload

import (
	"bufio"
	"bytes"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"google.golang.org/protobuf/encoding/protodelim"
	"google.golang.org/protobuf/proto"

	pb "github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/gen/vibesys/evaluator/v1"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/internal/files"
)

// counters is a test workload: keys with counters; a write sets a key to one more than the
// model's value, which Build updates, so a stream is only reproduced when every Build runs in
// order on the phase's own model.
type counters struct {
	values map[int]int
	phase  int
}

type info struct {
	Key   int `json:"key"`
	Value int `json:"value"`
}

func counterWorkload() Workload[int, *counters] {
	return Workload[int, *counters]{
		Dataset: func(path string) (int, error) {
			data, err := os.ReadFile(path)
			return len(data), err
		},
		Model: func(size int, _ *Rand) (*counters, error) {
			if size == 0 {
				return nil, errors.New("empty dataset")
			}
			return &counters{values: map[int]int{}}, nil
		},
		BeginPhase: func(m *counters, phase int) error {
			m.phase = phase
			return nil
		},
		Operations: []Operation[*counters]{
			{Name: "read", Weight: 3,
				Build: func(m *counters, r *Rand) Request {
					key := r.Below(4)
					return Request{Steps: []Step{Get(fmt.Sprintf("/k%d", key))}, Info: info{Key: key}}
				}},
			{Name: "write", Weight: 1,
				Build: func(m *counters, r *Rand) Request {
					key := r.Below(4)
					m.values[key]++
					value := m.values[key] + 1000*m.phase
					return Request{Steps: []Step{HTTP("PATCH", fmt.Sprintf("/k%d", key), Body([]byte(fmt.Sprint(value))))},
						Info: info{Key: key, Value: value}}
				}},
		},
	}
}

// datasetFile is a dataset file in dir.
func datasetFile(t *testing.T, dir string) string {
	t.Helper()
	path := filepath.Join(dir, "dataset.json")
	if err := os.WriteFile(path, []byte(`{"keys":4}`), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func phaseInput(t *testing.T, dir string, phase *pb.PhaseInput) string {
	t.Helper()
	data, err := proto.Marshal(phase)
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(dir, "phase-input.binpb")
	if err := os.WriteFile(path, data, 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

// stream runs `generate` until n operations were read, then closes the stream as loadgen does,
// and returns the stream's frames after the FileHeader.
func stream(t *testing.T, w Workload[int, *counters], input, state string, n int) [][]byte {
	t.Helper()
	read, write := io.Pipe()
	done := make(chan int)
	var stderr bytes.Buffer
	go func() {
		done <- run(w, []string{"generate", "--phase-input", input, "--dataset", datasetFile(t, state), "--state", state}, write, &stderr)
		_ = write.Close()
	}()
	in := bufio.NewReader(read)
	var frames [][]byte
	for range n + 2 {
		size, err := binary.ReadUvarint(in)
		if err != nil {
			t.Fatalf("%v; stderr %s", err, stderr.String())
		}
		frame := make([]byte, size)
		if _, err := io.ReadFull(in, frame); err != nil {
			t.Fatal(err)
		}
		frames = append(frames, frame)
	}
	_ = read.Close()
	if code := <-done; code != exitOK {
		t.Fatalf("generate exited %d: %s", code, stderr.String())
	}
	return frames[1:]
}

// The stream is a function of the PhaseInput: two runs write the same bytes, a Build's change to
// the model shapes the later operations (write values count up per key), the phase index reaches
// the model, and a benchmark keeps no bodies and no records.
func TestGenerateWritesADeterministicStream(t *testing.T) {
	dir := t.TempDir()
	input := phaseInput(t, dir, &pb.PhaseInput{SampleSeed: 7, PhaseIndex: 1})
	a, b := t.TempDir(), t.TempDir()
	first, second := stream(t, counterWorkload(), input, a, 500), stream(t, counterWorkload(), input, b, 500)
	header := &pb.RequestsHeader{}
	if err := proto.Unmarshal(first[0], header); err != nil || strings.Join(header.GetOperationNames(), ",") != "read,write" {
		t.Fatalf("header %v %v", header, err)
	}
	latest := map[string]int{}
	writes := 0
	for i := 1; i < len(first); i++ {
		if !bytes.Equal(first[i], second[i]) {
			t.Fatalf("operation %d differs between two runs", i-1)
		}
		op := &pb.PlannedOperation{}
		if err := proto.Unmarshal(first[i], op); err != nil {
			t.Fatal(err)
		}
		if op.GetKeepBodies() || op.GetExclusiveKey() != "" {
			t.Fatalf("operation %d keeps bodies or has an exclusive key: %v", i-1, op)
		}
		if op.GetOperation() == 1 {
			writes++
			call := op.GetSteps()[0].GetHttp()
			key := call.GetPath().GetParts()[0].GetLiteral()
			value := 0
			fmt.Sscan(string(call.GetRaw()), &value)
			if _, seen := latest[key]; !seen {
				latest[key] = 1000 // phase 1's values count from 1000
			}
			if call.GetMethod() != pb.HttpMethod_HTTP_METHOD_PATCH || value != latest[key]+1 {
				t.Fatalf("operation %d: %s set to %d after %d", i-1, key, value, latest[key])
			}
			latest[key] = value
		}
	}
	if writes == 0 {
		t.Fatal("no writes")
	}
	if entries, err := os.ReadDir(a); err != nil || len(entries) != 1 { // the dataset file only
		t.Fatalf("a benchmark's generator left %v in --state (%v)", entries, err)
	}
}

// Under accuracy capture every operation keeps its bodies and has a KeptOperation, with its info,
// in stream order; the stream's requests are a benchmark's.
func TestCaptureKeepsEveryOperation(t *testing.T) {
	dir := t.TempDir()
	plain := stream(t, counterWorkload(), phaseInput(t, dir, &pb.PhaseInput{SampleSeed: 3}), t.TempDir(), 300)
	state := t.TempDir()
	captured := stream(t, counterWorkload(), phaseInput(t, dir, &pb.PhaseInput{SampleSeed: 3, Capture: true}), state, 300)
	file, err := os.Open(filepath.Join(state, keptFile))
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = file.Close() }()
	in := bufio.NewReader(file)
	for i := 1; i < len(captured); i++ {
		op, base := &pb.PlannedOperation{}, &pb.PlannedOperation{}
		if err := proto.Unmarshal(captured[i], op); err != nil {
			t.Fatal(err)
		}
		_ = proto.Unmarshal(plain[i], base)
		if !op.GetKeepBodies() {
			t.Fatalf("operation %d does not keep its bodies", i-1)
		}
		op.KeepBodies = false
		if !proto.Equal(op, base) {
			t.Fatalf("operation %d differs from the benchmark's", i-1)
		}
		kept := &pb.KeptOperation{}
		if err := protodelim.UnmarshalFrom(in, kept); err != nil {
			t.Fatalf("kept %d: %v", i-1, err)
		}
		if kept.GetIndex() != uint64(i-1) || kept.GetOperation() != op.GetOperation() || !strings.HasPrefix(kept.GetInfoJson(), `{"key":`) {
			t.Fatalf("kept %d: %v", i-1, kept)
		}
	}
}

func toMessages(results []*pb.OperationResult) []proto.Message {
	out := make([]proto.Message, len(results))
	for i, r := range results {
		out[i] = r
	}
	return out
}

func writeFile(t *testing.T, path string, kind pb.FileKind, messages ...proto.Message) {
	t.Helper()
	file, err := os.Create(path)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = file.Close() }()
	writer, err := files.NewWriter(file, files.Binpb, files.Header(kind, "test"))
	if err != nil {
		t.Fatal(err)
	}
	for _, m := range messages {
		if err := writer.Write(m); err != nil {
			t.Fatal(err)
		}
	}
	if err := writer.Flush(); err != nil {
		t.Fatal(err)
	}
}

func TestUsageAndWorkloadErrors(t *testing.T) {
	var stderr bytes.Buffer
	if code := run(counterWorkload(), []string{"check", "--phase-input", "x"}, io.Discard, &stderr); code != exitUsage ||
		!strings.Contains(stderr.String(), "there is no check command") {
		t.Fatalf("exit %d: %s", code, stderr.String())
	}
	stderr.Reset()
	if code := run(counterWorkload(), []string{"generate", "--phase-input", "x", "--dataset", "d", "--state", "y", "--results", "r"}, io.Discard, &stderr); code != exitUsage {
		t.Fatalf("exit %d: %s", code, stderr.String())
	}
	bad := counterWorkload()
	bad.Operations[1].Weight = 0
	stderr.Reset()
	if code := run(bad, []string{"generate", "--phase-input", "x", "--dataset", "d", "--state", "y"}, io.Discard, &stderr); code != exitFailure ||
		!strings.Contains(stderr.String(), "weight must be a finite number > 0") {
		t.Fatalf("exit %d: %s", code, stderr.String())
	}
	dotted := counterWorkload()
	dotted.Operations[0].Name = "config.create"
	var stream bytes.Buffer
	stderr.Reset()
	if code := run(dotted, []string{"generate", "--phase-input", "x", "--dataset", "d", "--state", "y"}, &stream, &stderr); code != exitFailure ||
		!strings.Contains(stderr.String(), `operation name "config.create" is not one loadgen takes`) || stream.Len() != 0 {
		t.Fatalf("exit %d, %d bytes written: %s", code, stream.Len(), stderr.String())
	}
	panics := counterWorkload()
	panics.Operations[0].Build = func(*counters, *Rand) Request { panic("no keys") }
	dir := t.TempDir()
	input := phaseInput(t, dir, &pb.PhaseInput{SampleSeed: 1})
	stderr.Reset()
	if code := run(panics, []string{"generate", "--phase-input", input, "--dataset", datasetFile(t, dir), "--state", dir}, io.Discard, &stderr); code != exitFailure ||
		!strings.Contains(stderr.String(), "operation read: Build panicked: no keys") {
		t.Fatalf("exit %d: %s", code, stderr.String())
	}
	refuses := counterWorkload()
	refuses.BeginPhase = func(*counters, int) error { return errors.New("phase 0 has no keys") }
	stderr.Reset()
	if code := run(refuses, []string{"generate", "--phase-input", input, "--dataset", datasetFile(t, dir), "--state", dir}, io.Discard, &stderr); code != exitFailure ||
		!strings.Contains(stderr.String(), "Workload BeginPhase: phase 0 has no keys") {
		t.Fatalf("exit %d: %s", code, stderr.String())
	}
	empty := filepath.Join(dir, "empty")
	_ = os.WriteFile(empty, nil, 0o600)
	stderr.Reset()
	if code := run(counterWorkload(), []string{"generate", "--phase-input", input, "--dataset", empty, "--state", dir}, io.Discard, &stderr); code != exitFailure ||
		!strings.Contains(stderr.String(), "Workload Model: empty dataset") {
		t.Fatalf("exit %d: %s", code, stderr.String())
	}
}
