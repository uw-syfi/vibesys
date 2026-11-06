package workload

import (
	"bufio"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"

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
	// keptFile is the generator's record of every operation under accuracy capture, in --state.
	keptFile = "kept.binpb"
)

const usageText = `usage:
  GENERATOR generate --phase-input F --dataset F --state DIR
`

// Main runs the generator command in os.Args and exits: `generate` writes the phase's request
// stream on standard output until it is closed (sdk/vibesys-evaluator/docs/spec.md, "Generator
// command line"). Standard error carries only logs. A task's generator binary is
// `func main() { workload.Main(w) }`.
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
	switch args[0] {
	case "generate":
	case "check":
		return usageError{errors.New("there is no check command: a benchmark only measures, and an accuracy check judges the responses (sdk/vibesys-evaluator/docs/spec.md)")}
	default:
		return usageError{fmt.Errorf("unknown command %q", args[0])}
	}
	set := flag.NewFlagSet(args[0], flag.ContinueOnError)
	set.SetOutput(io.Discard)
	input := set.String("phase-input", "", "the PhaseInput file")
	state := set.String("state", "", "a directory for the generator's own files")
	datasetPath := set.String("dataset", "", "the task's dataset file")
	if err := set.Parse(args[1:]); err != nil {
		return usageError{err}
	}
	if set.NArg() > 0 {
		return usageError{fmt.Errorf("unexpected argument %q", set.Arg(0))}
	}
	for name, value := range map[string]string{"phase-input": *input, "state": *state, "dataset": *datasetPath} {
		if value == "" {
			return usageError{fmt.Errorf("--%s is required", name)}
		}
	}
	if err := w.validate(); err != nil {
		return err
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
	signal.Ignore(syscall.SIGPIPE) // a closed stream is the end of the phase: see generate
	return generate(w, data, phase, *state, stdout)
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

// newModel builds the phase's model and tells it the phase.
func newModel[D, M any](w Workload[D, M], data D, phase *pb.PhaseInput) (M, error) {
	var model M
	var err error
	if panicked := safely("Workload Model", func() { model, err = w.Model(data, NewRand(phase.GetSampleSeed(), "model")) }); panicked != nil {
		return model, panicked
	}
	if err != nil {
		return model, fmt.Errorf("Workload Model: %w", err)
	}
	if w.BeginPhase != nil {
		if panicked := safely("Workload BeginPhase", func() { err = w.BeginPhase(model, int(phase.GetPhaseIndex())) }); panicked != nil {
			return model, panicked
		}
		if err != nil {
			return model, fmt.Errorf("Workload BeginPhase: %w", err)
		}
	}
	return model, nil
}

// generate writes the request stream of the phase on out until out is closed. Operation i is
// drawn from the phase stream Rand(seed, "phase", index) by weight; Build gets the stream's
// Child(i). Under accuracy capture (PhaseInput.capture) every operation keeps its response bodies
// and is first kept, with its info, in state.
func generate[D, M any](w Workload[D, M], data D, phase *pb.PhaseInput, state string, out io.Writer) error {
	model, err := newModel(w, data, phase)
	if err != nil {
		return err
	}
	var kept *bufio.Writer
	closeKept := func() error { return nil }
	if phase.GetCapture() {
		keptOut, err := os.Create(filepath.Join(state, keptFile))
		if err != nil {
			return err
		}
		kept = bufio.NewWriterSize(keptOut, 1<<16)
		closeKept = func() error {
			if err := kept.Flush(); err != nil {
				return err
			}
			return keptOut.Close()
		}
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

func writeStream[D, M any](w Workload[D, M], model M, phase *pb.PhaseInput, out io.Writer, kept *bufio.Writer) error {
	names := make([]string, len(w.Operations))
	weights := make([]float64, len(w.Operations))
	for i, op := range w.Operations {
		names[i], weights[i] = op.Name, op.Weight
	}
	writer, err := files.NewWriter(out, files.Binpb, files.Header(pb.FileKind_FILE_KIND_REQUESTS, writer()))
	if err != nil {
		return err
	}
	if err := writer.Write(&pb.RequestsHeader{OperationNames: names}); err != nil {
		return err
	}
	capture := kept != nil
	stream := NewRand(phase.GetSampleSeed(), "phase", phase.GetPhaseIndex())
	for index := uint64(0); ; index++ {
		choice := stream.Weighted(weights)
		op := &w.Operations[choice]
		var request Request
		if err := safely("operation "+op.Name+": Build", func() { request = op.Build(model, stream.Child(index)) }); err != nil {
			return err
		}
		message, err := planned(request, choice, capture)
		if err != nil {
			return fmt.Errorf("operation %s: %w", op.Name, err)
		}
		if capture {
			info, err := infoJSON(request.Info)
			if err != nil {
				return fmt.Errorf("operation %s: %w", op.Name, err)
			}
			// Kept before it is written: every operation loadgen reads has its record.
			if _, err := protodelim.MarshalTo(kept, &pb.KeptOperation{Index: index, Operation: uint32(choice), InfoJson: info}); err != nil {
				return err
			}
		}
		if err := writer.Write(message); err != nil {
			return err
		}
	}
}

// writer names the Go front end in the FileHeader of the streams it writes.
func writer() string { return "vibesys-evaluator-go " + version.Version }
