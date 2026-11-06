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
	"time"

	"google.golang.org/protobuf/proto"

	pb "github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/gen/vibesys/evaluator/v1"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/internal/files"
)

// counters is a test workload: keys with counters; a write sets a key to a value, a read checks
// the value seen. Its writes depend on the model (they add one to the latest value), so a
// stream is only reproduced when pending writes are applied in order.
type counters struct{ values *Versions[int, int] }

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
			return &counters{values: NewVersions[int, int](0)}, nil
		},
		Operations: []Operation[*counters]{
			{Name: "read", Weight: 3, Sample: 0.5,
				Build: func(m *counters, r *Rand) Request {
					key := r.Below(4)
					return Request{Steps: []Step{Get(fmt.Sprintf("/k%d", key))}, Info: info{Key: key}}
				},
				Check: func(m *counters, c Call) error {
					var i info
					if err := c.DecodeInfo(&i); err != nil {
						return err
					}
					got := 0
					fmt.Sscan(string(c.Responses[0].Body), &got)
					for _, v := range m.values.Allowed(i.Key, 0, c.Start, c.End) {
						if v == got {
							return nil
						}
					}
					return fmt.Errorf("k%d: %d", i.Key, got)
				}},
			{Name: "write", Weight: 1,
				Build: func(m *counters, r *Rand) Request {
					key := r.Below(4)
					value := m.values.Latest(key, 0) + 1
					return Request{Steps: []Step{Put(fmt.Sprintf("/k%d", key), Body([]byte(fmt.Sprint(value))))},
						Info: info{Key: key, Value: value}, Exclusive: fmt.Sprintf("k%d", key)}
				},
				Check: func(m *counters, c Call) error { return nil },
				Apply: func(m *counters, c Call) error {
					var i info
					if err := c.DecodeInfo(&i); err != nil {
						return err
					}
					m.values.Write(i.Key, i.Value, c)
					return nil
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

// The stream is a function of the PhaseInput: two runs write the same bytes, and the writes'
// pending Apply shapes the later operations (write values count up per key).
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
		if op.GetOperation() == 1 {
			writes++
			key := op.GetExclusiveKey()
			value := 0
			fmt.Sscan(string(op.GetSteps()[0].GetHttp().GetRaw()), &value)
			if value != latest[key]+1 || !op.GetKeepBodies() {
				t.Fatalf("operation %d: %s set to %d after %d", i-1, key, value, latest[key])
			}
			latest[key] = value
		}
	}
	kept, err := readKept(filepath.Join(a, keptFile))
	if err != nil || writes == 0 {
		t.Fatalf("%d writes, %v", writes, err)
	}
	for index, k := range kept {
		if k.GetOperation() == 0 && !k.GetSampled() {
			t.Fatalf("an unsampled read %d was kept", index)
		}
	}
}

// check: the phase's sent writes go to the history in start order, with their info; a read
// that saw a value no write allows is a violation of the window; the warmup's are reported only.
func TestCheckFindsViolationsAndCollectsWrites(t *testing.T) {
	dir, state := t.TempDir(), t.TempDir()
	input := phaseInput(t, dir, &pb.PhaseInput{SampleSeed: 3})
	frames := stream(t, counterWorkload(), input, state, 300)
	kept, err := readKept(filepath.Join(state, keptFile))
	if err != nil {
		t.Fatal(err)
	}
	base := time.Unix(1_700_000_000, 0).UnixNano()
	window := [2]int64{base + 100*int64(time.Second), base + 200*int64(time.Second)}
	// Operation i runs over [base + 1 s * i, + 1 ms]: one at a time; reads see what a correct
	// store would show, except the first sampled read of the warmup (i < 100) and of the window
	// (100 <= i < 200), which see 99.
	values := map[string]int{}
	var results []*pb.OperationResult
	wrong := map[uint64]bool{}
	var wrongWarmup, wrongWindow int = -1, -1
	for i := range 200 {
		k := kept[uint64(i)]
		if k == nil || k.GetOperation() != 0 || !k.GetSampled() {
			continue
		}
		if i < 100 && wrongWarmup < 0 {
			wrongWarmup = i
		} else if i >= 100 && wrongWindow < 0 {
			wrongWindow = i
		}
	}
	for i, frame := range frames[1:] {
		op := &pb.PlannedOperation{}
		_ = proto.Unmarshal(frame, op)
		at := base + int64(i)*int64(time.Second)
		path := op.GetSteps()[0].GetHttp().GetPath().GetParts()[0].GetLiteral()
		body := []byte(fmt.Sprint(values[path]))
		if op.GetOperation() == 1 {
			v := 0
			fmt.Sscan(string(op.GetSteps()[0].GetHttp().GetRaw()), &v)
			values[path], body = v, nil
		} else if i == wrongWarmup || i == wrongWindow {
			body, wrong[uint64(i)] = []byte("99"), true
		}
		results = append(results, &pb.OperationResult{Index: uint64(i), Operation: op.GetOperation(),
			ScheduledUnixNanos: at, StartUnixNanos: at, EndUnixNanos: at + int64(time.Millisecond),
			Steps: []*pb.StepResult{{Status: 200, Response: &pb.StepResult_Body{Body: body}}}})
	}
	// Results arrive in completion order, not start order.
	results[10], results[11] = results[11], results[10]
	resultsPath, summaryPath, outputPath := filepath.Join(dir, "results"), filepath.Join(dir, "summary"), filepath.Join(dir, "output")
	writeFile(t, resultsPath, pb.FileKind_FILE_KIND_RESULTS, toMessages(results)...)
	summary := &pb.PhaseSummary{WindowStartUnixNanos: window[0], WindowEndUnixNanos: window[1]}
	writeFile(t, summaryPath, pb.FileKind_FILE_KIND_SUMMARY, summary)
	var stderr bytes.Buffer
	code := run(counterWorkload(), []string{"check", "--phase-input", input, "--dataset", datasetFile(t, dir), "--state", state, "--results", resultsPath,
		"--summary", summaryPath, "--output", outputPath}, io.Discard, &stderr)
	if code != exitOK {
		t.Fatalf("check exited %d: %s", code, stderr.String())
	}
	output := &pb.PhaseOutput{}
	if err := readMessage(outputPath, output); err != nil {
		t.Fatal(err)
	}
	if !proto.Equal(output.GetSummary(), summary) {
		t.Fatalf("summary %v", output.GetSummary())
	}
	var last int64
	for _, w := range output.GetWrites() {
		if w.GetOperation() != "write" || w.GetStartUnixNanos() < last || !strings.Contains(w.GetInfoJson(), `"value"`) {
			t.Fatalf("write %v after %d", w, last)
		}
		last = w.GetStartUnixNanos()
	}
	var messages []string
	for _, v := range output.GetViolations() {
		messages = append(messages, fmt.Sprint(v.GetIndex(), ":", v.GetMessage()))
	}
	if len(wrong) != 2 || output.GetInvalid() != 1 || len(messages) != 2 ||
		!strings.HasPrefix(messages[0], fmt.Sprintf("%d:", wrongWindow)) || !strings.HasPrefix(messages[1], fmt.Sprintf("%d:(warmup)", wrongWarmup)) {
		t.Fatalf("wrong %v; invalid %d, checked %d, violations %v", wrong, output.GetInvalid(), output.GetChecked(), messages)
	}
	if output.GetChecked() == 0 {
		t.Fatal("nothing checked")
	}
	// The history replays: a later phase's generate builds on these writes.
	next := phaseInput(t, t.TempDir(), &pb.PhaseInput{SampleSeed: 3, PhaseIndex: 1, History: output.GetWrites()})
	if frames := stream(t, counterWorkload(), next, t.TempDir(), 10); len(frames) != 11 {
		t.Fatalf("%d frames", len(frames))
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
	if code := run(counterWorkload(), []string{"check", "--phase-input", "x"}, io.Discard, &stderr); code != exitUsage {
		t.Fatalf("exit %d", code)
	}
	bad := counterWorkload()
	bad.Operations[1].Sample = 0.5
	stderr.Reset()
	if code := run(bad, []string{"generate", "--phase-input", "x", "--dataset", "d", "--state", "y"}, io.Discard, &stderr); code != exitFailure ||
		!strings.Contains(stderr.String(), "always checked") {
		t.Fatalf("exit %d: %s", code, stderr.String())
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
	empty := filepath.Join(dir, "empty")
	_ = os.WriteFile(empty, nil, 0o600)
	stderr.Reset()
	if code := run(counterWorkload(), []string{"generate", "--phase-input", input, "--dataset", empty, "--state", dir}, io.Discard, &stderr); code != exitFailure ||
		!strings.Contains(stderr.String(), "Workload Model: empty dataset") {
		t.Fatalf("exit %d: %s", code, stderr.String())
	}
}
