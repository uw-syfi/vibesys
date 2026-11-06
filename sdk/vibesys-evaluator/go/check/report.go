package check

import (
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"time"
)

// Verdict is a check's outcome.
type Verdict string

const (
	// Pass: every spec holds on every partition.
	Pass Verdict = "PASS"
	// Fail: some response is wrong on its own, some partition has no order the model accepts, or
	// too many outcomes are unknown.
	Fail Verdict = "FAIL"
	// Unknown: no failure was found, but something could not be decided (a budget ran out, a task
	// or capture bug). It is never a pass.
	Unknown Verdict = "UNKNOWN"
)

// ExitCode is the accuracy command's exit status for the verdict: 0 PASS, 1 FAIL, 2 UNKNOWN.
func (v Verdict) ExitCode() int {
	switch v {
	case Pass:
		return 0
	case Fail:
		return 1
	}
	return 2
}

// Status is what happened to one partition.
type Status string

const (
	StatusOK       Status = "ok"
	StatusIllegal  Status = "illegal"
	StatusTimeout  Status = "timeout"
	StatusMemory   Status = "memory"
	StatusTooLarge Status = "too-large"
	// StatusSkipped: the total budget ran out before the partition's search started.
	StatusSkipped Status = "skipped"
)

// Report is the result of a check.
type Report struct {
	Verdict Verdict
	// Reason is one paragraph that says why, and what to do for a verdict other than PASS.
	Reason  string
	Specs   []*SpecReport
	Elapsed time.Duration
	// PeakHeapBytes is the highest heap the budget watchdog saw.
	PeakHeapBytes int64
}

// SpecReport is one spec's part of a Report.
type SpecReport struct {
	Name     string
	Verdict  Verdict
	Reason   string
	Coverage Coverage
	// MaxInFlight is the most ops of one partition in flight at once.
	MaxInFlight int
	Partitions  []PartitionReport
	Failures    []Failure
	Violations  []Violation
	// LostShareExceeded: more pieces had an unknown outcome than Options.MaxLostShare allows.
	LostShareExceeded bool
	// Errors are problems of the task or the capture, not of the candidate: the verdict is
	// UNKNOWN unless the spec also failed.
	Errors   []string
	Warnings []string
	// HeapBeforeSearch is the heap when the searches started: what projection holds.
	HeapBeforeSearch int64
}

// Coverage says what was and was not checked.
type Coverage struct {
	// Calls read, and those with a lost response.
	Calls, LostCalls int
	// Pieces projected, those with an unknown outcome, and the unknown reads dropped.
	Pieces, UnknownPieces, UnknownReadsDropped int
	Keys, Faults                               int
	// ReadsSampledOut are reads dropped by MaxReadsPerKey.
	ReadsSampledOut int
	// OpsChecked are in partitions with a verdict (ok or illegal), OpsUnchecked in the others.
	OpsChecked, OpsUnchecked int
	ByStatus                 map[Status]int
}

// PartitionReport is what happened to one partition.
type PartitionReport struct {
	Name string
	Keys int
	// Ops searched (after sampling): writes and reads, of which Unknowns have an unknown outcome.
	Ops, Writes, Reads, Unknowns int
	SampledOut                   int
	// MaxInFlight is the most ops in flight at once: what makes a search hard.
	MaxInFlight int
	Status      Status
	Reason      string
	Duration    time.Duration
	Steps       int64
	// PeakHeapBytes is the checker's whole heap at its highest while this partition was searched.
	PeakHeapBytes int64
	abortHeap     int64
}

// Failure explains one failing partition.
type Failure struct {
	Partition   string
	Explanation string
	// Timeline is porcupine's HTML timeline of the failing prefix, nil when it is too large.
	Timeline []byte
}

func (s *SpecReport) finish(o Options) {
	s.Coverage.ByStatus = map[Status]int{}
	var illegal, undecided []PartitionReport
	for _, p := range s.Partitions {
		s.Coverage.ByStatus[p.Status]++
		s.MaxInFlight = max(s.MaxInFlight, p.MaxInFlight)
		switch p.Status {
		case StatusOK:
			s.Coverage.OpsChecked += p.Ops
		case StatusIllegal:
			s.Coverage.OpsChecked += p.Ops
			illegal = append(illegal, p)
		default:
			s.Coverage.OpsUnchecked += p.Ops
			undecided = append(undecided, p)
		}
	}
	if s.MaxInFlight > o.OverlapWarning {
		s.Warnings = append(s.Warnings, fmt.Sprintf("up to %d ops of one partition were in flight at once (keep it at about %d or fewer: a search that must reject a history costs about 2^overlap per op; lower the check's clients or rate per key)", s.MaxInFlight, o.OverlapWarning))
	}
	c := s.Coverage
	switch {
	case len(s.Violations) > 0 || len(illegal) > 0 || s.LostShareExceeded:
		s.Verdict = Fail
		var parts []string
		if n := len(s.Violations); n > 0 {
			parts = append(parts, fmt.Sprintf("%d responses are wrong on their own (first: %s: %s)", n, where(s.Violations[0].Call), s.Violations[0].Message))
		}
		if s.LostShareExceeded {
			parts = append(parts, fmt.Sprintf("%d of %d pieces (%.2f%%) have an unknown outcome (lost responses: %d of %d calls), more than MaxLostShare %.2f%%; at the check's fixed load, losing responses is the candidate's fault", c.UnknownPieces, c.Pieces, 100*float64(c.UnknownPieces)/float64(c.Pieces), c.LostCalls, c.Calls, 100*o.MaxLostShare))
		}
		if len(illegal) > 0 {
			var names []string
			for _, p := range illegal[:min(5, len(illegal))] {
				names = append(names, p.Name)
			}
			parts = append(parts, fmt.Sprintf("%d partitions have no order of their operations that the model accepts (%s)", len(illegal), strings.Join(names, "; ")))
		}
		s.Reason = strings.Join(parts, "; ")
		if len(undecided) > 0 {
			s.Reason += fmt.Sprintf(" (%d other partitions undecided)", len(undecided))
		}
	case len(s.Errors) > 0 || len(undecided) > 0:
		s.Verdict = Unknown
		var parts []string
		if n := len(s.Errors); n > 0 {
			parts = append(parts, fmt.Sprintf("%d problems of the task or the capture, not of the candidate (first: %s)", n, s.Errors[0]))
		}
		if len(undecided) > 0 {
			sort.SliceStable(undecided, func(i, j int) bool { return undecided[i].Ops > undecided[j].Ops })
			p := undecided[0]
			parts = append(parts, fmt.Sprintf("%d of %d partitions undecided (%d of %d ops unchecked); the largest, %q: %s; %d ops (%d writes, %d reads, %d unknown outcomes), up to %d in flight at once. %s",
				len(undecided), len(s.Partitions), c.OpsUnchecked, c.OpsChecked+c.OpsUnchecked,
				p.Name, p.Reason, p.Ops, p.Writes, p.Reads, p.Unknowns, p.MaxInFlight, advice(p, o, s.HeapBeforeSearch)))
		}
		s.Reason = strings.Join(parts, "; ")
	default:
		s.Verdict = Pass
		s.Reason = fmt.Sprintf("%d partitions, %d ops checked, %d reads sampled out, %d faults", len(s.Partitions), c.OpsChecked, c.ReadsSampledOut, c.Faults)
	}
}

func advice(p PartitionReport, o Options, projected int64) string {
	switch p.Status {
	case StatusTooLarge:
		return fmt.Sprintf("Lower the check's write rate on this key or its phases' length, or raise MaxOpsPerPartition (%d).", o.MaxOpsPerPartition)
	case StatusMemory:
		if projected > o.MemoryBytes/2 {
			return fmt.Sprintf("The pieces alone held %s of the %s budget when the searches started: check less history, project smaller inputs and outputs, or raise MemoryBytes.", mb(projected), mb(o.MemoryBytes))
		}
		fallthrough
	case StatusTimeout:
		if p.MaxInFlight > o.OverlapWarning {
			return fmt.Sprintf("A search is exponential in the ops in flight at once: lower the check's clients or the share of operations on this key (up to %d at once), or lower MaxReadsPerKey (%d).", p.MaxInFlight, o.MaxReadsPerKey)
		}
		if p.Unknowns > 0 {
			return "Unknown outcomes multiply the states a search tracks: set the spec's EffectWindow, or find why responses were lost."
		}
		return fmt.Sprintf("Raise the budget (PartitionTimeout %v, MemoryBytes %s), lower MaxReadsPerKey (%d), or make states that differ only in how they were reached compare equal, so the search's cache merges them.", o.PartitionTimeout, mb(o.MemoryBytes), o.MaxReadsPerKey)
	case StatusSkipped:
		return fmt.Sprintf("Raise TotalTimeout (%v) or Workers, or check less history.", o.TotalTimeout)
	}
	return ""
}

func (r *Report) finish() {
	verdict := Pass
	var reasons []string
	for _, s := range r.Specs {
		switch {
		case s.Verdict == Fail:
			verdict = Fail
		case s.Verdict == Unknown && verdict == Pass:
			verdict = Unknown
		}
	}
	for _, s := range r.Specs {
		if s.Verdict == verdict || verdict == Pass {
			reasons = append(reasons, fmt.Sprintf("spec %s: %s", s.Name, s.Reason))
		}
	}
	if len(r.Specs) == 0 {
		verdict = Unknown
		reasons = append(reasons, "no spec to check")
	}
	r.Verdict = verdict
	r.Reason = fmt.Sprintf("%s: %s.", verdict, strings.TrimSuffix(strings.Join(reasons, "; "), "."))
}

// Text renders the report: the verdict, then per spec its coverage, partitions (the undecided
// and failing first, at most maxPartitions), violations and explanations.
func (r *Report) Text(maxPartitions int) string {
	var sb strings.Builder
	fmt.Fprintf(&sb, "%s\n", r.Reason)
	fmt.Fprintf(&sb, "elapsed %v, checker peak heap %s\n", r.Elapsed.Round(time.Millisecond), mb(r.PeakHeapBytes))
	for _, s := range r.Specs {
		c := s.Coverage
		fmt.Fprintf(&sb, "\n== spec %s: %s\n", s.Name, s.Verdict)
		fmt.Fprintf(&sb, "coverage: %d calls (%d lost) -> %d pieces (%d unknown outcomes, %d unknown reads dropped) over %d keys in %d partitions; %d faults; ops checked %d, unchecked %d; reads sampled out %d; most in flight at once %d; partitions by status %v; heap before the searches %s\n",
			c.Calls, c.LostCalls, c.Pieces, c.UnknownPieces, c.UnknownReadsDropped, c.Keys, len(s.Partitions), c.Faults, c.OpsChecked, c.OpsUnchecked, c.ReadsSampledOut, s.MaxInFlight, c.ByStatus, mb(s.HeapBeforeSearch))
		for _, w := range s.Warnings {
			fmt.Fprintf(&sb, "warning: %s\n", w)
		}
		for i, e := range s.Errors {
			if i >= 10 {
				fmt.Fprintf(&sb, "... %d more errors\n", len(s.Errors)-i)
				break
			}
			fmt.Fprintf(&sb, "error: %s\n", e)
		}
		for i, v := range s.Violations {
			if i >= 10 {
				fmt.Fprintf(&sb, "... %d more violations\n", len(s.Violations)-i)
				break
			}
			fmt.Fprintf(&sb, "violation: %s: %s\n", where(v.Call), v.Message)
		}
		if len(s.Partitions) > 0 {
			parts := append([]PartitionReport(nil), s.Partitions...)
			sort.SliceStable(parts, func(i, j int) bool {
				if (parts[i].Status == StatusOK) != (parts[j].Status == StatusOK) {
					return parts[i].Status != StatusOK
				}
				return parts[i].Duration > parts[j].Duration
			})
			fmt.Fprintf(&sb, "%-34s %7s %7s %7s %5s %6s %8s %-9s %12s %10s %9s\n", "partition", "ops", "writes", "reads", "unk", "smpl", "inflight", "status", "time", "steps", "peakheap")
			for i, p := range parts {
				if i >= maxPartitions {
					fmt.Fprintf(&sb, "... %d more partitions\n", len(parts)-i)
					break
				}
				name := p.Name
				if len(name) > 34 {
					name = name[:31] + "..."
				}
				fmt.Fprintf(&sb, "%-34s %7d %7d %7d %5d %6d %8d %-9s %12v %10d %9s\n", name, p.Ops, p.Writes, p.Reads, p.Unknowns, p.SampledOut, p.MaxInFlight, p.Status, p.Duration.Round(time.Microsecond), p.Steps, mb(p.PeakHeapBytes))
				if p.Reason != "" {
					fmt.Fprintf(&sb, "    %s\n", p.Reason)
				}
			}
		}
		for _, f := range s.Failures {
			fmt.Fprintf(&sb, "--- failing partition %s\n%s\n", f.Partition, f.Explanation)
		}
	}
	return sb.String()
}

var unsafeName = regexp.MustCompile(`[^A-Za-z0-9_.-]+`)

// WriteFiles writes report.txt and each failure's timeline (<spec>-failure-<n>.html) into dir,
// creating it, and returns the paths written.
func (r *Report) WriteFiles(dir string) ([]string, error) {
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, err
	}
	var paths []string
	for _, s := range r.Specs {
		for i, f := range s.Failures {
			if f.Timeline == nil {
				continue
			}
			path := filepath.Join(dir, fmt.Sprintf("%s-failure-%d.html", unsafeName.ReplaceAllString(s.Name, "_"), i))
			if err := os.WriteFile(path, f.Timeline, 0o644); err != nil {
				return paths, err
			}
			paths = append(paths, path)
		}
	}
	path := filepath.Join(dir, "report.txt")
	text := r.Text(50)
	if len(paths) > 0 {
		text += "\ntimelines: " + strings.Join(paths, ", ") + "\n"
	}
	if err := os.WriteFile(path, []byte(text), 0o644); err != nil {
		return paths, err
	}
	return append([]string{path}, paths...), nil
}
