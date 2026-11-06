package workload

import (
	"bufio"
	"cmp"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"os/signal"
	"path/filepath"
	"slices"
	"syscall"
	"time"
	"unicode/utf8"

	"google.golang.org/protobuf/encoding/protodelim"
	"google.golang.org/protobuf/proto"

	pb "github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/gen/vibesys/evaluator/v1"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/internal/files"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/internal/version"
)

const (
	exitOK      = 0
	exitFailure = 1
	exitUsage   = 2
	// keptFile is the generator's record of its sampled operations and writes, in --state.
	keptFile      = "kept.binpb"
	maxViolations = 20
	maxMessage    = 2000
)

const usageText = `usage:
  GENERATOR generate --phase-input F --dataset F --state DIR
  GENERATOR check --phase-input F --dataset F --state DIR --results F --summary F --output F
`

// Main runs the generator command in os.Args and exits: `generate` writes the phase's request
// stream on standard output until it is closed, and `check` checks the phase's results and writes
// its PhaseOutput (sdk/vibesys-evaluator/docs/spec.md, "Generator command line"). Standard error
// carries only logs. A task's generator binary is `func main() { workload.Main(w) }`.
func Main[D, M any](w Workload[D, M]) {
	os.Exit(run(w, os.Args[1:], os.Stdout, os.Stderr))
}

func run[D, M any](w Workload[D, M], args []string, stdout io.Writer, stderr io.Writer) int {
	err := dispatch(w, args, stdout)
	if err == nil {
		return exitOK
	}
	fmt.Fprintln(stderr, "generator:", err)
	var usage usageError
	if errors.As(err, &usage) {
		fmt.Fprint(stderr, usageText)
		return exitUsage
	}
	return exitFailure
}

type usageError struct{ err error }

func (e usageError) Error() string { return e.err.Error() }

func dispatch[D, M any](w Workload[D, M], args []string, stdout io.Writer) error {
	if len(args) == 0 {
		return usageError{errors.New("no command")}
	}
	set := flag.NewFlagSet(args[0], flag.ContinueOnError)
	set.SetOutput(io.Discard)
	input := set.String("phase-input", "", "the PhaseInput file")
	state := set.String("state", "", "a directory generate and check share")
	datasetPath := set.String("dataset", "", "the task's dataset file")
	results := set.String("results", "", "check: loadgen's results file")
	summary := set.String("summary", "", "check: loadgen's summary file")
	output := set.String("output", "", "check: the PhaseOutput file to write")
	if err := set.Parse(args[1:]); err != nil {
		return usageError{err}
	}
	if set.NArg() > 0 {
		return usageError{fmt.Errorf("unexpected argument %q", set.Arg(0))}
	}
	required := map[string]string{"phase-input": *input, "state": *state, "dataset": *datasetPath}
	if args[0] == "check" {
		required["results"], required["summary"], required["output"] = *results, *summary, *output
	}
	for name, value := range required {
		if value == "" {
			return usageError{fmt.Errorf("--%s is required", name)}
		}
	}
	if err := w.validate(); err != nil {
		return err
	}
	if args[0] != "generate" && args[0] != "check" {
		return usageError{fmt.Errorf("unknown command %q", args[0])}
	}
	phase := &pb.PhaseInput{}
	if err := readMessage(*input, phase); err != nil {
		return err
	}
	var data D
	if w.Dataset != nil {
		var err error
		if data, err = w.Dataset(*datasetPath); err != nil {
			return fmt.Errorf("Workload Dataset(%s): %w", *datasetPath, err)
		}
	}
	switch args[0] {
	case "generate":
		signal.Ignore(syscall.SIGPIPE) // a closed stream is the end of the phase: see writeStream
		return generate(w, data, phase, *state, stdout)
	case "check":
		return check(w, data, phase, checkFiles{state: *state, results: *results, summary: *summary, output: *output})
	}
	return nil
}

func readMessage(path string, message proto.Message) error {
	data, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	if err := proto.Unmarshal(data, message); err != nil {
		return fmt.Errorf("%s: %w", path, err)
	}
	return nil
}

// safely runs f, turning a panic into an error naming what panicked.
func safely(what string, f func()) (err error) {
	defer func() {
		if r := recover(); r != nil {
			err = fmt.Errorf("%s panicked: %v", what, r)
		}
	}()
	f()
	return nil
}

func infoJSON(info any) (string, error) {
	if info == nil {
		return "{}", nil
	}
	text, err := json.Marshal(info)
	if err != nil {
		return "", fmt.Errorf("Request Info is not JSON: %w", err)
	}
	if len(text) == 0 || text[0] != '{' {
		return "", fmt.Errorf("Request Info is a JSON object (a map, a struct or an Object), got %s", text)
	}
	return string(text), nil
}

// MarshalJSON writes the object with its keys in order.
func (o Object) MarshalJSON() ([]byte, error) {
	out := []byte{'{'}
	for i, field := range o {
		if i > 0 {
			out = append(out, ',')
		}
		key, err := json.Marshal(field.Key)
		if err != nil {
			return nil, err
		}
		value, err := json.Marshal(field.Value)
		if err != nil {
			return nil, err
		}
		out = append(append(append(out, key...), ':'), value...)
	}
	return append(out, '}'), nil
}

// newModel builds the model and replays the sample's history through each write's Apply.
func newModel[D, M any](w Workload[D, M], data D, phase *pb.PhaseInput) (M, error) {
	var model M
	var err error
	if panicked := safely("Workload Model", func() { model, err = w.Model(data, NewRand(phase.GetSampleSeed(), "model")) }); panicked != nil {
		return model, panicked
	}
	if err != nil {
		return model, fmt.Errorf("Workload Model: %w", err)
	}
	return model, replay(w, model, phase.GetHistory())
}

// apply runs a write's Apply, naming the operation in its error or panic.
func apply[M any](op *Operation[M], model M, call Call) error {
	var err error
	if panicked := safely("operation "+op.Name+": Apply", func() { err = op.Apply(model, call) }); panicked != nil {
		return panicked
	}
	if err != nil {
		return fmt.Errorf("operation %s: Apply: %w", op.Name, err)
	}
	return nil
}

func replay[D, M any](w Workload[D, M], model M, entries []*pb.HistoryEntry) error {
	byName := map[string]*Operation[M]{}
	for i := range w.Operations {
		byName[w.Operations[i].Name] = &w.Operations[i]
	}
	for _, entry := range entries {
		op := byName[entry.GetOperation()]
		if op == nil || op.Apply == nil {
			return fmt.Errorf("the history has a write of %q, which is not a write of this workload", entry.GetOperation())
		}
		call := callOf(entry.GetInfoJson(), entry.GetStartUnixNanos(), entry.GetEndUnixNanos(), entry.GetOk(), entry.GetSteps())
		if err := apply(op, model, call); err != nil {
			return err
		}
	}
	return nil
}

func callOf(info string, start, end int64, ok bool, steps []*pb.StepResult) Call {
	if info == "" {
		info = "{}"
	}
	responses := make([]Response, len(steps))
	for i, s := range steps {
		responses[i] = Response{Status: int(s.GetStatus()), Body: s.GetBody()}
	}
	return Call{Info: json.RawMessage(info), Start: time.Unix(0, start), End: time.Unix(0, end), OK: ok, Responses: responses}
}

// generate writes the request stream of the phase on out until out is closed. Operation i is
// drawn from the phase stream Rand(seed, "phase", index) by weight, then whether it is in the
// validation sample; Build gets the stream's Child(i); a write's Apply runs at once with a
// pending Call. The sampled operations and the writes are kept in state for check.
func generate[D, M any](w Workload[D, M], data D, phase *pb.PhaseInput, state string, out io.Writer) error {
	model, err := newModel(w, data, phase)
	if err != nil {
		return err
	}
	keptOut, err := os.Create(filepath.Join(state, keptFile))
	if err != nil {
		return err
	}
	kept := bufio.NewWriterSize(keptOut, 1<<16)
	closeKept := func() error {
		if err := kept.Flush(); err != nil {
			return err
		}
		return keptOut.Close()
	}
	err = writeStream(w, model, phase, out, kept)
	if errors.Is(err, syscall.EPIPE) || errors.Is(err, os.ErrClosed) || errors.Is(err, io.ErrClosedPipe) {
		err = nil // loadgen closed the stream: the phase is over
	}
	if closeErr := closeKept(); err == nil {
		err = closeErr
	}
	return err
}

func writeStream[D, M any](w Workload[D, M], model M, phase *pb.PhaseInput, out io.Writer, kept io.Writer) error {
	names := make([]string, len(w.Operations))
	weights := make([]float64, len(w.Operations))
	shares := make([]float64, len(w.Operations))
	for i, op := range w.Operations {
		names[i], weights[i], shares[i] = op.Name, op.Weight, op.checkedShare()
	}
	writer, err := files.NewWriter(out, files.Binpb, files.Header(pb.FileKind_FILE_KIND_REQUESTS, writer()))
	if err != nil {
		return err
	}
	if err := writer.Write(&pb.RequestsHeader{OperationNames: names}); err != nil {
		return err
	}
	stream := NewRand(phase.GetSampleSeed(), "phase", phase.GetPhaseIndex())
	for index := uint64(0); ; index++ {
		choice := stream.Weighted(weights)
		sampled := stream.Random() < shares[choice]
		op := &w.Operations[choice]
		var request Request
		if err := safely("operation "+op.Name+": Build", func() { request = op.Build(model, stream.Child(index)) }); err != nil {
			return err
		}
		write := op.Apply != nil
		message, err := planned(request, choice, sampled || write)
		if err != nil {
			return fmt.Errorf("operation %s: %w", op.Name, err)
		}
		if sampled || write {
			info, err := infoJSON(request.Info)
			if err != nil {
				return fmt.Errorf("operation %s: %w", op.Name, err)
			}
			// Kept before it is written: every operation loadgen reads has its record.
			if _, err := protodelim.MarshalTo(kept, &pb.KeptOperation{Index: index, Operation: uint32(choice), InfoJson: info, Sampled: sampled}); err != nil {
				return err
			}
			if write {
				if err := apply(op, model, Call{Info: json.RawMessage(info), Pending: true}); err != nil {
					return err
				}
			}
		}
		if err := writer.Write(message); err != nil {
			return err
		}
	}
}

// writer names the Go front end in the FileHeader of the streams it writes.
func writer() string { return "vibesys-evaluator-go " + version.Version }

type checkFiles struct{ state, results, summary, output string }

// sent is what check keeps of one result: a write sent, or a sampled operation that succeeded.
type sent struct {
	result *pb.OperationResult
	kept   *pb.KeptOperation
}

// check checks the sample and collects this phase's writes into a PhaseOutput: a fresh model,
// the history and this phase's sent writes applied in start order, then Check on every sampled
// operation that loadgen counted as a success, in start order.
func check[D, M any](w Workload[D, M], data D, phase *pb.PhaseInput, paths checkFiles) error {
	began := time.Now()
	kept, err := readKept(filepath.Join(paths.state, keptFile))
	if err != nil {
		return err
	}
	summary, err := readSummary(paths.summary)
	if err != nil {
		return err
	}
	calls, err := readSent(w, paths.results, kept)
	if err != nil {
		return err
	}
	model, err := newModel(w, data, phase)
	if err != nil {
		return err
	}
	var writes []*pb.HistoryEntry
	for _, c := range calls {
		if w.Operations[c.result.GetOperation()].Apply != nil {
			r := c.result
			writes = append(writes, &pb.HistoryEntry{
				Operation: w.Operations[r.GetOperation()].Name, InfoJson: c.kept.GetInfoJson(),
				StartUnixNanos: r.GetStartUnixNanos(), EndUnixNanos: r.GetEndUnixNanos(),
				Ok: r.GetError() == pb.ErrorKind_ERROR_KIND_UNSPECIFIED, Steps: r.GetSteps(),
			})
		}
	}
	if err := replay(w, model, writes); err != nil {
		return err
	}
	tally := &tally{}
	for _, c := range calls {
		r := c.result
		op := &w.Operations[r.GetOperation()]
		if !c.kept.GetSampled() || r.GetError() != pb.ErrorKind_ERROR_KIND_UNSPECIFIED || op.Check == nil {
			continue
		}
		call := callOf(c.kept.GetInfoJson(), r.GetStartUnixNanos(), r.GetEndUnixNanos(), true, r.GetSteps())
		var problem error
		if err := safely("check", func() { problem = op.Check(model, call) }); err != nil {
			problem = err
		}
		at := r.GetScheduledUnixNanos()
		tally.add(op.Name, r.GetIndex(), problem, summary.GetWindowStartUnixNanos() <= at && at < summary.GetWindowEndUnixNanos())
	}
	output := &pb.PhaseOutput{
		Summary: summary, Checked: tally.checked, Invalid: tally.invalid,
		Violations: append(tally.window, tally.warmup...)[:min(maxViolations, len(tally.window)+len(tally.warmup))],
		Writes:     writes, CheckSecs: time.Since(began).Seconds(),
	}
	encoded, err := proto.Marshal(output)
	if err != nil {
		return err
	}
	return os.WriteFile(paths.output, encoded, 0o644)
}

type tally struct {
	checked, invalid uint64
	window, warmup   []*pb.Violation
}

func (t *tally) add(operation string, index uint64, problem error, inWindow bool) {
	if inWindow {
		t.checked++
	}
	if problem == nil {
		return
	}
	kept, message := &t.warmup, "(warmup) "+problem.Error()
	if inWindow {
		t.invalid++
		kept, message = &t.window, problem.Error()
	}
	if len(*kept) < maxViolations {
		*kept = append(*kept, &pb.Violation{Operation: operation, Index: index, Message: truncate(message, maxMessage)})
	}
}

func truncate(text string, runes int) string {
	if utf8.RuneCountInString(text) <= runes {
		return text
	}
	return string([]rune(text)[:runes])
}

func readKept(path string) (map[uint64]*pb.KeptOperation, error) {
	file, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer func() { _ = file.Close() }()
	in := bufio.NewReaderSize(file, 1<<16)
	kept := map[uint64]*pb.KeptOperation{}
	for {
		message := &pb.KeptOperation{}
		err := protodelim.UnmarshalFrom(in, message)
		if errors.Is(err, io.EOF) && !errors.Is(err, io.ErrUnexpectedEOF) {
			return kept, nil
		}
		if err != nil {
			// The generator was stopped while writing its last record; loadgen never read the
			// operation after it.
			if errors.Is(err, io.ErrUnexpectedEOF) {
				return kept, nil
			}
			return nil, fmt.Errorf("%s: %w", path, err)
		}
		kept[message.GetIndex()] = message
	}
}

func readSummary(path string) (*pb.PhaseSummary, error) {
	file, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer func() { _ = file.Close() }()
	reader, _, err := files.NewReader(file, files.Binpb, pb.FileKind_FILE_KIND_SUMMARY)
	if err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	summary := &pb.PhaseSummary{}
	if err := reader.Read(summary); err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	return summary, nil
}

// readSent reads the results, keeping the writes loadgen sent (start not 0: an operation never
// sent cannot have taken effect) and the sampled operations, in order of start time.
func readSent[D, M any](w Workload[D, M], path string, kept map[uint64]*pb.KeptOperation) ([]sent, error) {
	file, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer func() { _ = file.Close() }()
	reader, _, err := files.NewReader(file, files.Binpb, pb.FileKind_FILE_KIND_RESULTS)
	if err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	var calls []sent
	for {
		result := &pb.OperationResult{}
		err := reader.Read(result)
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			return nil, fmt.Errorf("%s: %w", path, err)
		}
		if int(result.GetOperation()) >= len(w.Operations) {
			return nil, fmt.Errorf("%s: operation %d of result %d is not in the workload", path, result.GetOperation(), result.GetIndex())
		}
		record, ok := kept[result.GetIndex()]
		write := w.Operations[result.GetOperation()].Apply != nil
		if write && !ok {
			return nil, fmt.Errorf("%s: no kept record of write %d (the generator's --state is not this phase's)", path, result.GetIndex())
		}
		if !ok || result.GetStartUnixNanos() == 0 {
			continue
		}
		if record.GetOperation() != result.GetOperation() {
			return nil, fmt.Errorf("%s: result %d is operation %d, the generator kept %d (the generator's --state is not this phase's)", path, result.GetIndex(), result.GetOperation(), record.GetOperation())
		}
		if write || (record.GetSampled() && result.GetError() == pb.ErrorKind_ERROR_KIND_UNSPECIFIED) {
			calls = append(calls, sent{result: result, kept: record})
		}
	}
	slices.SortFunc(calls, func(a, b sent) int {
		return cmp.Or(cmp.Compare(a.result.GetStartUnixNanos(), b.result.GetStartUnixNanos()), cmp.Compare(a.result.GetIndex(), b.result.GetIndex()))
	})
	return calls, nil
}
