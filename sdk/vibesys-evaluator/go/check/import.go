package check

import (
	"errors"
	"fmt"
	"io"
	"math"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"

	"google.golang.org/protobuf/proto"

	pb "github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/gen/vibesys/evaluator/v1"
)

// faultKind is FaultEvent.kind's rule.
var faultKind = regexp.MustCompile(`^[a-z][a-z0-9_]{0,62}$`)

// WriteHistory makes a history directory (see OpenHistory) at dir from loadgen phase
// directories, in phase order, to check runs captured before the accuracy capture wrote
// histories. Each phase directory has loadgen's results (results.binpb, or results.binpb.gz as
// a phase's artifacts keep it) and summary.binpb, and the generator's kept.binpb (or
// state/kept.binpb) with a record of every operation sent. Files are hard-linked where the file
// system allows and copied otherwise.
//
// faults are the faults between phases. A fault without times (Start and End 0) is placed
// between the last response of phase AfterPhase and the first call of the next, which is where
// it happened when nothing better was recorded; End 0 alone means a point in time at Start.
// dataset is the dataset file the generator read, or "" for none. dir must be empty or not
// exist.
func WriteHistory(dir string, phases []string, faults []Fault, dataset string) error {
	if len(phases) == 0 {
		return errors.New("no phase directories")
	}
	if entries, err := os.ReadDir(dir); err == nil && len(entries) > 0 {
		return fmt.Errorf("%s is not empty", dir)
	}
	m := &pb.HistoryManifest{Phases: uint32(len(phases))}
	spans := map[int][2]int64{}
	span := func(i int) ([2]int64, error) {
		if s, ok := spans[i]; ok {
			return s, nil
		}
		first, last, err := phaseSpan(phases[i])
		spans[i] = [2]int64{first, last}
		return spans[i], err
	}
	for _, f := range faults {
		switch {
		case !faultKind.MatchString(f.Kind):
			return fmt.Errorf("fault kind %q: want lower case letters, digits and _, starting with a letter", f.Kind)
		case len(f.Target) > 1024:
			return fmt.Errorf("fault %s: target longer than 1024 bytes", f.Kind)
		case f.AfterPhase < 0 || f.AfterPhase >= len(phases):
			return fmt.Errorf("fault %s %s: after phase %d of %d", f.Kind, f.Target, f.AfterPhase, len(phases))
		}
		if f.Start == 0 && f.End == 0 {
			before, err := span(f.AfterPhase)
			if err != nil {
				return err
			}
			f.Start, f.End = before[1]+1, before[1]+1
			if f.AfterPhase+1 < len(phases) {
				after, err := span(f.AfterPhase + 1)
				if err != nil {
					return err
				}
				f.End = after[0] - 1
			}
			if f.End < f.Start {
				return fmt.Errorf("fault %s %s: phases %d and %d overlap; give the fault's times", f.Kind, f.Target, f.AfterPhase, f.AfterPhase+1)
			}
		} else if f.End == 0 {
			f.End = f.Start
		}
		if f.End < f.Start {
			return fmt.Errorf("fault %s %s ends before it starts", f.Kind, f.Target)
		}
		m.Faults = append(m.Faults, &pb.FaultEvent{Kind: f.Kind, Target: f.Target, StartUnixNanos: f.Start, EndUnixNanos: f.End, AfterPhase: uint32(f.AfterPhase)})
	}
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	for i, src := range phases {
		dst := filepath.Join(dir, phaseDir(i))
		if err := os.MkdirAll(dst, 0o755); err != nil {
			return err
		}
		results := resultsFile
		if _, err := os.Stat(filepath.Join(src, results)); errors.Is(err, os.ErrNotExist) {
			results = resultsFile + ".gz"
		}
		kept := filepath.Join(src, keptFile)
		if _, err := os.Stat(kept); errors.Is(err, os.ErrNotExist) {
			kept = filepath.Join(src, "state", keptFile)
		}
		for _, f := range [][2]string{
			{filepath.Join(src, results), results},
			{filepath.Join(src, summaryFile), summaryFile},
			{kept, keptFile},
		} {
			if err := linkOrCopy(f[0], filepath.Join(dst, f[1])); err != nil {
				return fmt.Errorf("phase %d: %w", i, err)
			}
		}
	}
	if dataset != "" {
		m.Dataset = filepath.Base(dataset)
		if err := linkOrCopy(dataset, filepath.Join(dir, m.Dataset)); err != nil {
			return err
		}
	}
	raw, err := proto.Marshal(m)
	if err != nil {
		return err
	}
	if err := os.WriteFile(filepath.Join(dir, manifestFile), raw, 0o644); err != nil {
		return err
	}
	_, err = OpenHistory(dir)
	return err
}

// phaseSpan is the first call and the last response of the operations a phase sent.
func phaseSpan(dir string) (first, last int64, err error) {
	r, path, closeFn, err := openResults(dir)
	if err != nil {
		return 0, 0, err
	}
	defer closeFn()
	first, last = math.MaxInt64, math.MinInt64
	for {
		res := &pb.OperationResult{}
		err := r.Read(res)
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			return 0, 0, fmt.Errorf("%s: %w", path, err)
		}
		if res.GetStartUnixNanos() == 0 {
			continue
		}
		first = min(first, res.GetStartUnixNanos())
		last = max(last, res.GetEndUnixNanos(), res.GetStartUnixNanos())
	}
	if first > last {
		return 0, 0, fmt.Errorf("%s: no operation was sent", path)
	}
	return first, last, nil
}

func linkOrCopy(src, dst string) error {
	if err := os.Link(src, dst); err == nil {
		return nil
	}
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()
	out, err := os.OpenFile(dst, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o644)
	if err != nil {
		return err
	}
	if _, err := io.Copy(out, in); err != nil {
		out.Close()
		return err
	}
	return out.Close()
}

// runImport is Main's import command.
func runImport(args []string, stdout, stderr io.Writer) int {
	fs := newFlagSet("oracle import", stderr)
	out := fs.String("out", "", "the history directory to write (required; empty or new)")
	dataset := fs.String("dataset", "", "the dataset file the generator read")
	var faults []Fault
	fs.Func("fault", "a fault between phases: kind=K,after=N[,target=T][,start=TIME][,end=TIME] (repeatable; TIME is Unix nanoseconds or RFC 3339; no times: between the phases)", func(v string) error {
		f, err := parseFault(v)
		faults = append(faults, f)
		return err
	})
	if err := fs.Parse(args); err != nil {
		return 2
	}
	if *out == "" || fs.NArg() == 0 {
		fmt.Fprintln(stderr, "usage: oracle import --out DIR [--dataset FILE] [--fault SPEC]... PHASE_DIR...")
		return 2
	}
	if err := WriteHistory(*out, fs.Args(), faults, *dataset); err != nil {
		fmt.Fprintf(stderr, "import: %v\n", err)
		return 2
	}
	fmt.Fprintf(stdout, "wrote the history %s: %d phases, %d faults\n", *out, fs.NArg(), len(faults))
	return 0
}

func parseFault(v string) (Fault, error) {
	f := Fault{AfterPhase: -1}
	for _, field := range strings.Split(v, ",") {
		key, value, ok := strings.Cut(field, "=")
		if !ok {
			return f, fmt.Errorf("%q: want key=value", field)
		}
		var err error
		switch key {
		case "kind":
			f.Kind = value
		case "target":
			f.Target = value
		case "after":
			f.AfterPhase, err = strconv.Atoi(value)
		case "start":
			f.Start, err = parseTime(value)
		case "end":
			f.End, err = parseTime(value)
		default:
			return f, fmt.Errorf("%q: unknown key %q (kind, target, after, start, end)", v, key)
		}
		if err != nil {
			return f, fmt.Errorf("%s: %w", key, err)
		}
	}
	if f.Kind == "" || f.AfterPhase < 0 {
		return f, fmt.Errorf("%q: kind and after are required", v)
	}
	return f, nil
}

func parseTime(v string) (int64, error) {
	if n, err := strconv.ParseInt(v, 10, 64); err == nil {
		return n, nil
	}
	t, err := time.Parse(time.RFC3339Nano, v)
	if err != nil {
		return 0, fmt.Errorf("%q is neither Unix nanoseconds nor RFC 3339", v)
	}
	return t.UnixNano(), nil
}
