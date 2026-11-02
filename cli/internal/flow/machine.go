package flow

import (
	"errors"
	"fmt"
	"path"
	"slices"
	"sort"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/reading"
	"github.com/uw-syfi/vibesys/cli/internal/task"
)

// Request is the one thing the machine asks the shell to do next. The shell does it and answers
// with an Event, whatever happens: a request that is never answered holds the machine for good.
type Request interface{ request() }

// Checkout asks for a clean checkout of the job's commit and a scratch directory outside it.
type Checkout struct{}

// Run asks for a command to run in the checkout.
type Run struct {
	Stage   Stage
	Argv    []string
	Timeout time.Duration
	// Read, when set, is a file to read after the command ends, whatever its outcome.
	Read string
}

// Observe asks for the machine's facts and its load now.
type Observe struct{}

func (Checkout) request() {}
func (Run) request()      {}
func (Observe) request()  {}

// Event is the answer to the last request, or news the shell has: a cancel.
type Event interface{ event() }

// CheckedOut answers Checkout. Dir is the checkout and Scratch a directory for the files the
// benchmark writes.
type CheckedOut struct{ Dir, Scratch string }

// CheckoutFailed answers Checkout when no checkout could be made.
type CheckoutFailed struct{ Failure core.Failure }

// Ran answers Run with how the command ended, the times the shell saw it start and end, and the
// file the request asked to read. Found says the file was there, and ReadError why a file that
// was there could not be read, such as being too large. A command that did not run to its end can
// leave the file missing or partial, which only matters to a command that exited 0.
type Ran struct {
	Exec      core.ExecOutcome
	Started   time.Time
	Ended     time.Time
	Output    []byte
	Found     bool
	ReadError string
}

// NotRan answers Run when the command never started, such as an environment that is not there.
type NotRan struct{ Failure core.Failure }

// Unseen answers Run when the command started and nobody can say how it ended, such as an
// environment whose container was lost under it. It is never NotRan: what started may have done
// anything.
type Unseen struct{ Failure core.Failure }

// Observed answers Observe. A machine that could not be read leaves its fields zero: the facts
// and the load are recorded and never acted on.
type Observed struct {
	Machine core.MachineFacts
	Load    core.MachineLoad
}

// Cancelled says the job was cancelled while no command was running: the shell stopped before
// its next request. A command that was running when the cancel came answers with Ran and
// ExecCancelled instead; the machine treats both alike.
type Cancelled struct{}

func (CheckedOut) event()     {}
func (CheckoutFailed) event() {}
func (Ran) event()            {}
func (NotRan) event()         {}
func (Unseen) event()         {}
func (Observed) event()       {}
func (Cancelled) event()      {}

// Verdict is how the job ended: Passed, NotAttempted, Rejected, Stopped or Lost. The set is closed, so a
// job that passed carries its findings and nothing else, and one that did not pass carries none.
// It is nil until the machine is done.
type Verdict interface{ verdict() }

// Passed says every step ran and the job found what it was asked for.
type Passed struct{ Findings Findings }

// NotAttempted says a step could not be attempted, such as a checkout that could not be made.
type NotAttempted struct {
	Stage   Stage
	Failure core.Failure
	// Reason is a line for the job's output saying what ended it.
	Reason string
}

// Rejected says every command ran but the job has no acceptable result: the benchmark's output was
// missing or invalid. Reason says why.
type Rejected struct{ Reason string }

// Stopped says a step ended unlike a pass: a failing exit code, a time limit, a cancel.
type Stopped struct {
	Stage Stage
	// Exec is how the step ended.
	Exec core.ExecOutcome
	// Reason is a line for the job's output saying what ended it.
	Reason string
}

// Lost says a step's command started and nobody can say how it ended.
type Lost struct {
	Stage   Stage
	Failure core.Failure
	// Reason is a line for the job's output saying what ended it.
	Reason string
}

func (Passed) verdict()       {}
func (NotAttempted) verdict() {}
func (Rejected) verdict()     {}
func (Stopped) verdict()      {}
func (Lost) verdict()         {}

// Cancelled reports whether a cancel ended the job.
func (s Stopped) Cancelled() bool { return s.Exec.Kind == core.ExecCancelled }

// Ending says in which step a job that did not pass ended and gives the line for its output that
// says why. A job that passed, and a machine that is not done, have neither.
func Ending(v Verdict) (Stage, string) {
	switch v := v.(type) {
	case Passed:
		return "", ""
	case NotAttempted:
		return v.Stage, v.Reason
	case Rejected:
		return StageBenchmark, v.Reason
	case Stopped:
		return v.Stage, v.Reason
	case Lost:
		return v.Stage, v.Reason
	default:
		return "", ""
	}
}

type phase string

const (
	phaseCheckout phase = "checkout"
	phaseBuild    phase = "build"
	phaseCheck    phase = "check"
	phaseBefore   phase = "observe-before"
	phaseSample   phase = "sample"
	phaseAfter    phase = "observe-after"
	phaseDone     phase = "done"
)

// State is where the machine is. It is a value: Step returns a new one and leaves the old one as
// it was.
type State struct {
	plan    Plan
	phase   phase
	dir     string
	scratch string

	checkTook time.Duration
	machine   core.MachineFacts
	haveFacts bool
	samples   []float64
	others    []core.Observation
	verdict   Verdict
}

// Start returns the machine for a plan and its first request. It fails for a plan that is not
// valid.
func Start(p Plan) (State, Request, error) {
	if err := p.Validate(); err != nil {
		return State{}, nil, err
	}
	return State{plan: p, phase: phaseCheckout}, Checkout{}, nil
}

// Done reports whether the machine reached its verdict.
func (s State) Done() bool { return s.phase == phaseDone }

// Verdict returns how the job ended, and is nil until the machine is Done.
func (s State) Verdict() Verdict { return s.verdict }

// Phase names where the machine is, for diagnostics.
func (s State) Phase() string { return string(s.phase) }

// Step is the machine. It takes the event that answers the last request and returns the next
// state and the next request, which is nil once the machine is done. An event that does not
// answer the request the machine is waiting for is a bug in the shell, and ends the job as not
// attempted rather than guessing.
func Step(s State, e Event) (State, Request) {
	if s.phase == phaseDone {
		return s, nil
	}
	if _, ok := e.(Cancelled); ok {
		return s.cancelled(), nil
	}
	switch s.phase {
	case phaseCheckout:
		switch e := e.(type) {
		case CheckedOut:
			if e.Dir == "" || e.Scratch == "" {
				return s.refuse("the checkout answered without a directory"), nil
			}
			s.dir, s.scratch = e.Dir, e.Scratch
			return s.afterCheckout()
		case CheckoutFailed:
			f := e.Failure
			return s.end(NotAttempted{Stage: StageCheckout, Failure: f, Reason: "could not check out the commit: " + f.Message}), nil
		default:
			// An answer that is not a checkout's is refused with the rest, below.
		}
	case phaseBuild:
		if r, ok := e.(Ran); ok {
			if v, ended := s.commandEnded(StageBuild, r); ended {
				return s.end(v), nil
			}
			return s.afterBuild()
		}
		if v, ok := s.unrun(StageBuild, e); ok {
			return s.end(v), nil
		}
	case phaseCheck:
		if r, ok := e.(Ran); ok {
			if v, ended := s.commandEnded(StageCheck, r); ended {
				return s.end(v), nil
			}
			s.checkTook = r.Ended.Sub(r.Started)
			if s.checkTook < 0 {
				s.checkTook = 0
			}
			return s.afterCheck()
		}
		if v, ok := s.unrun(StageCheck, e); ok {
			return s.end(v), nil
		}
	case phaseBefore:
		if o, ok := e.(Observed); ok {
			s.observe(o)
			return s.nextSample()
		}
	case phaseSample:
		if r, ok := e.(Ran); ok {
			return s.sampled(r)
		}
		if v, ok := s.unrun(StageBenchmark, e); ok {
			return s.end(v), nil
		}
	case phaseAfter:
		if o, ok := e.(Observed); ok {
			s.observe(o)
			return s.passed(o), nil
		}
	}
	return s.refuse(fmt.Sprintf("the answer %T does not fit the %s step", e, s.phase)), nil
}

func (s State) afterCheckout() (State, Request) {
	if s.plan.Build != nil {
		s.phase = phaseBuild
		return s, s.run(StageBuild, *s.plan.Build, "")
	}
	return s.afterBuild()
}

func (s State) afterBuild() (State, Request) {
	if s.plan.RunsCheck() {
		s.phase = phaseCheck
		return s, s.run(StageCheck, s.plan.Check, "")
	}
	return s.afterCheck()
}

func (s State) afterCheck() (State, Request) {
	if s.plan.Kind == core.JobCheck {
		s.phase = phaseAfter
		return s, Observe{}
	}
	s.phase = phaseBefore
	return s, Observe{}
}

func (s State) nextSample() (State, Request) {
	s.phase = phaseSample
	out := path.Join(s.scratch, fmt.Sprintf("sample-%d.out", len(s.samples)))
	c := s.plan.Benchmark
	argv := append(c.Resolve(s.dir), s.plan.Result.OutputFlag(), out)
	return s, Run{Stage: StageBenchmark, Argv: argv, Timeout: c.Timeout, Read: out}
}

func (s State) run(stage Stage, c task.Command, read string) Run {
	return Run{Stage: stage, Argv: c.Resolve(s.dir), Timeout: c.Timeout, Read: read}
}

// observe keeps the first facts it is given: they do not change while the job runs.
func (s *State) observe(o Observed) {
	if !s.haveFacts {
		s.machine, s.haveFacts = o.Machine, true
	}
}

// sampled reads what one benchmark run left.
func (s State) sampled(r Ran) (State, Request) {
	n := len(s.samples)
	if v, ended := s.commandEnded(StageBenchmark, r); ended {
		return s.end(v), nil
	}
	got, err := s.read(r)
	if err != nil {
		var reported *reading.Reported
		why := fmt.Sprintf("sample %d: %v", n+1, err)
		if errors.As(err, &reported) {
			why = fmt.Sprintf("sample %d: %v", n+1, reported)
		}
		return s.end(Rejected{Reason: why}), nil
	}
	s.samples = append(slices.Clip(s.samples), got.Value)
	for _, o := range got.Others {
		s.others = addObservation(s.others, o)
	}
	if uint32(len(s.samples)) < s.plan.Samples {
		return s.nextSample()
	}
	s.phase = phaseAfter
	return s, Observe{}
}

// read applies the reader the plan names to what the run wrote.
func (s State) read(r Ran) (reading.Reading, error) {
	if r.ReadError != "" {
		return reading.Reading{}, fmt.Errorf("the result file could not be read: %s", r.ReadError)
	}
	if !r.Found {
		return reading.Reading{}, fmt.Errorf("the benchmark exited 0 but wrote no result to the path after %s", s.plan.Result.OutputFlag())
	}
	if s.plan.Result.Protocol2 {
		return reading.Protocol2(r.Output, s.plan.Metric.Name)
	}
	return reading.JSONField(r.Output, s.plan.Result.Field)
}

// addObservation returns the observations with the value added to its series, keeping them
// ordered by name and leaving the slice it was given as it was.
func addObservation(obs []core.Observation, v reading.Named) []core.Observation {
	out := slices.Clone(obs)
	i := sort.Search(len(out), func(i int) bool { return out[i].Name >= v.Name })
	if i < len(out) && out[i].Name == v.Name {
		out[i].Values = append(slices.Clip(slices.Clone(out[i].Values)), v.Value)
		return out
	}
	return slices.Insert(out, i, core.Observation{Name: v.Name, Values: []float64{v.Value}})
}

// commandEnded decides whether a command's ending ends the job. A command that exited 0 does
// not; any other ending does, and says how.
func (s State) commandEnded(stage Stage, r Ran) (Verdict, bool) {
	stopped := func(reason string) (Verdict, bool) {
		return Stopped{Stage: stage, Exec: r.Exec, Reason: reason}, true
	}
	switch {
	case r.Exec.Kind == core.ExecExited && r.Exec.ExitCode == 0:
		return nil, false
	case r.Exec.Kind == core.ExecExited:
		return stopped(fmt.Sprintf("the %s step exited with code %d", stage, r.Exec.ExitCode))
	case r.Exec.Kind == core.ExecTimedOut:
		return stopped(fmt.Sprintf("the %s step ran past its time limit and was ended", stage))
	case r.Exec.Kind == core.ExecCancelled:
		return stopped("the job was cancelled during the " + string(stage) + " step")
	}
	return stopped(fmt.Sprintf("the %s step ended in a way that is neither success nor failure (%s)", stage, r.Exec.Kind))
}

// unrun is the verdict on a Run answered without an ending: a command that never started, or one
// whose ending nobody saw. ok is false for any other answer.
func (s State) unrun(stage Stage, e Event) (v Verdict, ok bool) {
	switch e := e.(type) {
	case NotRan:
		f := e.Failure
		return NotAttempted{Stage: stage, Failure: f, Reason: fmt.Sprintf("the %s step could not be run: %s", stage, f.Message)}, true
	case Unseen:
		f := e.Failure
		return Lost{Stage: stage, Failure: f, Reason: fmt.Sprintf("the %s step ran, and how it ended is not known: %s", stage, f.Message)}, true
	default:
		// Any other answer says how the command ended, or is not an answer to Run.
		return nil, false
	}
}

func (s State) passed(o Observed) State {
	f := Findings{CheckTook: s.checkTook, Machine: s.machine, Load: o.Load}
	if s.plan.Kind == core.JobMeasure {
		f.Samples = slices.Clone(s.samples)
		f.Observations = s.others
	}
	return s.end(Passed{Findings: f})
}

func (s State) cancelled() State {
	return s.end(Stopped{Stage: s.stage(), Exec: core.ExecOutcome{Kind: core.ExecCancelled}, Reason: "the job was cancelled during the " + string(s.stage()) + " step"})
}

// refuse ends the job on a shell that broke the protocol.
func (s State) refuse(why string) State {
	f := core.Failure{Kind: core.FailureInternal, Message: "flow: " + why}
	return s.end(NotAttempted{Stage: s.stage(), Failure: f, Reason: f.Message})
}

// stage is the step the machine is in.
func (s State) stage() Stage {
	switch s.phase {
	case phaseCheckout:
		return StageCheckout
	case phaseBuild:
		return StageBuild
	case phaseCheck:
		return StageCheck
	case phaseSample:
		return StageBenchmark
	}
	return StageObserve
}

func (s State) end(v Verdict) State {
	s.phase = phaseDone
	s.verdict = v
	s.samples, s.others = nil, nil
	return s
}
