package check

import (
	"bufio"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"

	"google.golang.org/protobuf/encoding/protodelim"
	"google.golang.org/protobuf/proto"

	pb "github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/gen/vibesys/evaluator/v1"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/internal/files"
)

// Call is one operation a phase sent, as loadgen recorded it, with the generator's Info. Times
// are Unix nanoseconds of the loadgen clock.
type Call struct {
	// Phase is the phase's index, from 0.
	Phase int
	// Index is the operation's position in the phase's request stream.
	Index uint64
	// Client is the loadgen client that ran it (two operations of one client never overlap), or
	// -1 when the history does not record it.
	Client int
	// Op is the operation's name.
	Op string
	// Info is the Request's info, a JSON object.
	Info json.RawMessage
	// Start is when the first step was sent; End when the last step's response was complete or
	// the operation failed.
	Start, End int64
	// Steps are the steps that were sent, in order.
	Steps []Step
}

// Step is one step of a Call.
type Step struct {
	Start, End int64
	// Status is the HTTP status, 0 when no response arrived.
	Status int
	// Body is the response body; nil when it was not kept.
	Body []byte
	// Error describes a transport, timeout or capture error.
	Error string
}

// Lost says whether some step got no response (a transport error, a timeout, or the phase ended
// first): the operation may or may not have taken effect.
func (c *Call) Lost() bool {
	for _, s := range c.Steps {
		if s.Status == 0 {
			return true
		}
	}
	return len(c.Steps) == 0
}

// Fault is a fault injected between two phases, with no operation in flight from Start to End.
type Fault struct {
	// Kind is the task-level name of what was done ("restart", "flush").
	Kind string
	// Target is what it hit, for messages.
	Target string
	// Start and End are Unix nanoseconds of the loadgen clock.
	Start, End int64
	// AfterPhase is the phase it follows.
	AfterPhase int
}

// History is a captured run: its phases, loaded one at a time, and the faults between them.
type History struct {
	// Phases is the number of phases.
	Phases int
	// Faults are the faults injected between phases, in order.
	Faults []Fault
	// Dataset is the path of the dataset file the generator read, or "" when there is none.
	Dataset string
	load    func(i int) ([]Call, error)
}

// NewHistory is a history held in memory (tests, synthetic histories).
func NewHistory(phases [][]Call, faults []Fault) *History {
	return &History{Phases: len(phases), Faults: faults, load: func(i int) ([]Call, error) {
		return phases[i], nil
	}}
}

// Phase loads phase i's calls, ordered by start.
func (h *History) Phase(i int) ([]Call, error) {
	if i < 0 || i >= h.Phases {
		return nil, fmt.Errorf("phase %d: the history has %d phases", i, h.Phases)
	}
	return h.load(i)
}

// Files of a history directory (sdk/vibesys-evaluator/docs/spec.md, "Accuracy history").
const (
	manifestFile = "history.binpb"
	resultsFile  = "results.binpb"
	summaryFile  = "summary.binpb"
	keptFile     = "kept.binpb"
)

// OpenHistory opens a history directory: history.binpb (a HistoryManifest) and, per phase,
// phase-<i>/ with loadgen's results.binpb and summary.binpb and the generator's kept.binpb (or
// state/kept.binpb, where a phase pod leaves it). Without history.binpb it reads the phase-<i>
// directories it finds, with no faults, and dataset.json if there is one. Phases are read when
// Phase asks for them.
func OpenHistory(dir string) (*History, error) {
	h := &History{}
	raw, err := os.ReadFile(filepath.Join(dir, manifestFile))
	switch {
	case err == nil:
		m := &pb.HistoryManifest{}
		if err := proto.Unmarshal(raw, m); err != nil {
			return nil, fmt.Errorf("%s: %w", filepath.Join(dir, manifestFile), err)
		}
		if m.GetPhases() == 0 {
			return nil, fmt.Errorf("%s: phases is 0", filepath.Join(dir, manifestFile))
		}
		h.Phases = int(m.GetPhases())
		for _, f := range m.GetFaults() {
			h.Faults = append(h.Faults, Fault{Kind: f.GetKind(), Target: f.GetTarget(), Start: f.GetStartUnixNanos(), End: f.GetEndUnixNanos(), AfterPhase: int(f.GetAfterPhase())})
		}
		if m.GetDataset() != "" {
			h.Dataset = filepath.Join(dir, m.GetDataset())
		}
	case errors.Is(err, os.ErrNotExist):
		n, err := countPhases(dir)
		if err != nil {
			return nil, err
		}
		h.Phases = n
		if _, err := os.Stat(filepath.Join(dir, "dataset.json")); err == nil {
			h.Dataset = filepath.Join(dir, "dataset.json")
		}
	default:
		return nil, err
	}
	for i := range h.Phases {
		if _, err := os.Stat(filepath.Join(dir, phaseDir(i), resultsFile)); err != nil {
			return nil, fmt.Errorf("phase %d: %w", i, err)
		}
	}
	h.load = func(i int) ([]Call, error) { return readPhase(filepath.Join(dir, phaseDir(i)), i) }
	return h, nil
}

func phaseDir(i int) string { return "phase-" + strconv.Itoa(i) }

func countPhases(dir string) (int, error) {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return 0, err
	}
	seen := map[int]bool{}
	for _, e := range entries {
		if n, err := strconv.Atoi(strings.TrimPrefix(e.Name(), "phase-")); err == nil && e.IsDir() && strings.HasPrefix(e.Name(), "phase-") {
			seen[n] = true
		}
	}
	for i := range len(seen) {
		if !seen[i] {
			return 0, fmt.Errorf("%s: phase directories are not phase-0 to phase-%d", dir, len(seen)-1)
		}
	}
	if len(seen) == 0 {
		return 0, fmt.Errorf("%s: no %s and no phase-<i> directories", dir, manifestFile)
	}
	return len(seen), nil
}

// readPhase reads one phase directory into calls. Operations never sent (start 0) cannot have
// taken effect and are skipped; an operation sent without a kept record cannot be projected.
func readPhase(dir string, phase int) ([]Call, error) {
	summary := &pb.PhaseSummary{}
	if err := readSingle(filepath.Join(dir, summaryFile), pb.FileKind_FILE_KIND_SUMMARY, summary); err != nil {
		return nil, err
	}
	names := summary.GetOperationNames()
	keptPath := filepath.Join(dir, keptFile)
	if _, err := os.Stat(keptPath); errors.Is(err, os.ErrNotExist) {
		keptPath = filepath.Join(dir, "state", keptFile)
	}
	kept, err := readKept(keptPath)
	if err != nil {
		return nil, err
	}
	path := filepath.Join(dir, resultsFile)
	in, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer in.Close()
	r, _, err := files.NewReader(in, files.Binpb, pb.FileKind_FILE_KIND_RESULTS)
	if err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	var calls []Call
	for {
		res := &pb.OperationResult{}
		err := r.Read(res)
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			return nil, fmt.Errorf("%s: %w", path, err)
		}
		if res.GetStartUnixNanos() == 0 {
			continue
		}
		k, ok := kept[res.GetIndex()]
		if !ok {
			return nil, fmt.Errorf("%s: operation %d was sent but %s has no record of it (the accuracy capture keeps every operation)", path, res.GetIndex(), keptPath)
		}
		if int(res.GetOperation()) >= len(names) {
			return nil, fmt.Errorf("%s: operation %d names operation %d of %d", path, res.GetIndex(), res.GetOperation(), len(names))
		}
		c := Call{
			Phase: phase, Index: res.GetIndex(), Client: -1, Op: names[res.GetOperation()],
			Info: json.RawMessage(k), Start: res.GetStartUnixNanos(), End: res.GetEndUnixNanos(),
		}
		if res.Client != nil {
			c.Client = int(res.GetClient())
		}
		for _, s := range res.GetSteps() {
			step := Step{Start: s.GetStartUnixNanos(), End: max(s.GetEndUnixNanos(), s.GetStartUnixNanos()), Status: int(s.GetStatus()), Body: s.GetBody(), Error: s.GetErrorMessage()}
			if step.Error == "" && s.GetError() != pb.ErrorKind_ERROR_KIND_UNSPECIFIED {
				step.Error = s.GetError().String()
			}
			c.Steps = append(c.Steps, step)
		}
		c.End = max(c.End, c.Start)
		calls = append(calls, c)
	}
	sort.SliceStable(calls, func(i, j int) bool { return calls[i].Start < calls[j].Start })
	return calls, nil
}

func readSingle(path string, kind pb.FileKind, m proto.Message) error {
	in, err := os.Open(path)
	if err != nil {
		return err
	}
	defer in.Close()
	r, _, err := files.NewReader(in, files.Binpb, kind)
	if err != nil {
		return fmt.Errorf("%s: %w", path, err)
	}
	if err := r.Read(m); err != nil {
		return fmt.Errorf("%s: %w", path, err)
	}
	return nil
}

// readKept reads the generator's KeptOperations (length-delimited, no header) into each
// operation's info.
func readKept(path string) (map[uint64]string, error) {
	in, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer in.Close()
	r := bufio.NewReaderSize(in, 1<<16)
	out := map[uint64]string{}
	for {
		k := &pb.KeptOperation{}
		err := protodelim.UnmarshalFrom(r, k)
		// A generator stopped while writing leaves its last record cut short: it is the record of
		// an operation loadgen never read (records are written before their operations).
		if errors.Is(err, io.EOF) || errors.Is(err, io.ErrUnexpectedEOF) {
			return out, nil
		}
		if err != nil {
			return nil, fmt.Errorf("%s: %w", path, err)
		}
		out[k.GetIndex()] = k.GetInfoJson()
	}
}
