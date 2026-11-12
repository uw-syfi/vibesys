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
	// Candidate, for a task of two images, is the candidate's image and the server the command runs
	// against. The Timeout covers the server's startup time as well as the command's own limit.
	Candidate *core.CandidateRun
	// OnService says the command runs against the job-scoped server the last Serve started.
	OnService bool
	// List, when set, is a directory under the command's artifacts directory whose entries to
	// list after the command ends, whatever its outcome: the trace/ directory of a trace.
	List string
}

// Serve asks for the job-scoped server to be started and waited for until it is ready. A server
// the job started before has ended, when Again says so, and is to be ended first.
type Serve struct {
	// Argv is the server's command, with the checkout's root resolved and the port not.
	Argv []string
	// Ready is the HTTP path that answers 2xx once the server is ready.
	Ready string
	// StartupTimeout bounds the wait.
	StartupTimeout time.Duration
	// Lifetime is the longest the job may still use the server: its startup and every command
	// still to run against it.
	Lifetime time.Duration
	// Again says the job's earlier server ended, and this one replaces it.
	Again bool
}

// ProbeService asks whether the job-scoped server still runs.
type ProbeService struct{}

// Observe asks for the machine's facts and its load now.
type Observe struct{}

// BuildCandidate asks for the candidate's image of a task of two images, built from the job's
// commit unless an image of the same Dockerfile and context is there already.
type BuildCandidate struct{ Build core.CandidateBuild }

func (Checkout) request()       {}
func (Run) request()            {}
func (Observe) request()        {}
func (BuildCandidate) request() {}
func (Serve) request()          {}
func (ProbeService) request()   {}

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
	// ServiceLog is the last lines the candidate's server wrote, when it ended the run.
	ServiceLog string
	// Artifacts is the command's artifacts directory, relative to the repository's root when it is
	// inside it, and empty for a job that keeps no artifacts.
	Artifacts string
	// Listed are the names of the entries of the directory the request asked to list, sorted, and
	// empty when it is not there. ListError says why one that was there could not be listed.
	Listed    []string
	ListError string
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

// CandidateBuilt answers BuildCandidate with the image. A build that failed, or a Dockerfile the
// task does not allow, is answered with NotRan.
type CandidateBuilt struct{ Image core.CandidateImage }

// Serving answers Serve: the server runs and is ready. A server that could not be started, or was
// not ready, is answered as Run is when a command did not pass: with Ran and how the start ended,
// NotRan or Unseen.
type Serving struct{}

// ServiceState answers ProbeService: whether the server still runs.
type ServiceState struct{ Alive bool }

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
func (CandidateBuilt) event() {}
func (Serving) event()        {}
func (ServiceState) event()   {}
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
// missing or invalid, or a trace left nothing that can be read. Stage is the step whose output was
// refused, and Reason says why.
type Rejected struct {
	Stage  Stage
	Reason string
}

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
		return v.Stage, v.Reason
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
	phaseCheckout  phase = "checkout"
	phaseBuild     phase = "build"
	phaseCandidate phase = "candidate"
	phaseCheck     phase = "check"
	phaseBefore    phase = "observe-before"
	phaseSample    phase = "sample"
	phaseServe     phase = "serve"
	phaseProbe     phase = "probe-service"
	phaseReset     phase = "reset"
	phaseTrace     phase = "trace"
	phaseAfter     phase = "observe-after"
	phaseDone      phase = "done"
)

// State is where the machine is. It is a value: Step returns a new one and leaves the old one as
// it was.
type State struct {
	plan    Plan
	phase   phase
	dir     string
	scratch string

	checkTook time.Duration
	trace     traced
	candidate core.CandidateImage
	machine   core.MachineFacts
	haveFacts bool
	samples   []float64
	others    []core.Observation
	verdict   Verdict

	// The job-scoped server, for a plan with one. serving says one was started and is taken to
	// run; used that a command of the job ran against it; restarted that it replaced one that
	// ended. then is the phase a Serve leads to, and pending the server of the sample that runs.
	serving   bool
	used      bool
	restarted bool
	then      phase
	pending   core.SampleService
	services  []core.SampleService
}

// traced is what a trace job's command left.
type traced struct {
	took      time.Duration
	artifacts string
	files     []string
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
	case phaseCandidate:
		switch e := e.(type) {
		case CandidateBuilt:
			if !core.ValidImageDigest(e.Image.ID) {
				return s.refuse(fmt.Sprintf("the candidate's image was answered with %q, which is not an exact digest", e.Image.ID)), nil
			}
			s.candidate = e.Image
			return s.afterCandidate()
		case NotRan:
			f := e.Failure
			return s.end(NotAttempted{Stage: StageCandidate, Failure: f, Reason: "the candidate's image could not be built: " + f.Message}), nil
		default:
			// Any other answer is refused with the rest, below.
		}
	case phaseCheck:
		if r, ok := e.(Ran); ok {
			s.used = s.used || s.serving
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
	case phaseTrace:
		if r, ok := e.(Ran); ok {
			s.used = s.used || s.serving
			return s.traceRan(r)
		}
		if v, ok := s.unrun(StageTrace, e); ok {
			return s.end(v), nil
		}
	case phaseBefore:
		if o, ok := e.(Observed); ok {
			s.observe(o)
			return s.nextSample()
		}
	case phaseSample:
		if r, ok := e.(Ran); ok {
			s.used = s.used || s.serving
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
	case phaseServe:
		switch e := e.(type) {
		case Serving:
			s.serving, s.used = true, false
			return s.afterServe()
		case Ran:
			if v, ended := s.commandEnded(StageService, e); ended {
				return s.end(v), nil
			}
			// A start that ran to an exit of 0 is no answer to Serve: refused below.
		default:
			if v, ok := s.unrun(StageService, e); ok {
				return s.end(v), nil
			}
		}
	case phaseProbe:
		if st, ok := e.(ServiceState); ok {
			if !st.Alive {
				return s.serve(phaseSample, true)
			}
			s.phase = phaseReset
			return s, s.run(StageReset, s.plan.Service.Reset, "")
		}
	case phaseReset:
		if r, ok := e.(Ran); ok {
			if v, ended := s.commandEnded(StageReset, r); ended {
				if stopped, ok := v.(Stopped); ok {
					stopped.Reason += fmt.Sprintf(", before sample %d, which it fails", len(s.samples)+1)
					v = stopped
				}
				return s.end(v), nil
			}
			return s.runSample()
		}
		if v, ok := s.unrun(StageReset, e); ok {
			return s.end(v), nil
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
	if c := s.plan.Candidate; c != nil {
		s.phase = phaseCandidate
		return s, BuildCandidate{Build: c.Build}
	}
	return s.afterCandidate()
}

func (s State) afterCandidate() (State, Request) {
	if s.plan.RunsCheck() {
		if s.plan.serves() && s.plan.Service.ShareWithCheck {
			return s.serve(phaseCheck, false)
		}
		s.phase = phaseCheck
		return s, s.run(StageCheck, s.plan.Check, "")
	}
	return s.afterCheck()
}

// serve asks for the job-scoped server, which then leads to the phase then: the check or a sample.
// again says it replaces one that ended.
func (s State) serve(then phase, again bool) (State, Request) {
	s.phase, s.then = phaseServe, then
	s.serving, s.used, s.restarted = false, false, again
	sv := s.plan.Service
	return s, Serve{Argv: sv.ServeAt(s.dir), Ready: sv.Ready, StartupTimeout: sv.StartupTimeout, Lifetime: s.lifetime(then), Again: again}
}

// lifetime is the longest the job may use a server it starts now, which leads to the phase then:
// its startup, the check when it is next, and every sample still to take with the reset before it.
func (s State) lifetime(then phase) time.Duration {
	sv := s.plan.Service
	d := sv.StartupTimeout
	switch then {
	case phaseCheck:
		d += s.plan.Check.Timeout
	case phaseTrace:
		d += s.plan.Trace.Timeout
	}
	if left := int64(s.plan.Samples) - int64(len(s.samples)); left > 0 {
		d += time.Duration(left) * (s.plan.Benchmark.Timeout + sv.Reset.Timeout)
	}
	return d
}

// afterServe goes on with what the server was started for.
func (s State) afterServe() (State, Request) {
	switch s.then {
	case phaseCheck:
		s.phase = phaseCheck
		return s, s.run(StageCheck, s.plan.Check, "")
	case phaseTrace:
		return s.runTrace()
	}
	return s.runSample()
}

func (s State) afterCheck() (State, Request) {
	switch s.plan.Kind {
	case core.JobCheck:
		s.phase = phaseAfter
		return s, Observe{}
	case core.JobTrace:
		if s.plan.serves() {
			return s.serve(phaseTrace, false)
		}
		return s.runTrace()
	}
	s.phase = phaseBefore
	return s, Observe{}
}

// runTrace asks for the one run of a trace job's command, and for the entries of the trace/
// directory it leaves in its artifacts directory.
func (s State) runTrace() (State, Request) {
	s.phase = phaseTrace
	r := s.run(StageTrace, s.plan.Trace, "")
	r.List = core.TraceFilesDir
	return s, r
}

// traceRan reads what the trace command left: a trace that did not exit 0 ends the job in the
// trace stage, and one that did is a pass once the machine is observed, provided its artifacts
// directory is known and its trace/ directory could be read.
func (s State) traceRan(r Ran) (State, Request) {
	if v, ended := s.commandEnded(StageTrace, r); ended {
		return s.end(v), nil
	}
	switch {
	case r.Artifacts == "":
		return s.end(Rejected{Stage: StageTrace, Reason: "the trace exited 0, and this job keeps no artifacts directory for its reports"}), nil
	case r.ListError != "":
		return s.end(Rejected{Stage: StageTrace, Reason: fmt.Sprintf("the trace exited 0, and its %s/ directory could not be read: %s", core.TraceFilesDir, r.ListError)}), nil
	case len(r.Listed) > core.MaxTraceFiles:
		return s.end(Rejected{Stage: StageTrace, Reason: fmt.Sprintf("the trace exited 0, and left %d entries in its %s/ directory, more than %d", len(r.Listed), core.TraceFilesDir, core.MaxTraceFiles)}), nil
	}
	took := r.Ended.Sub(r.Started)
	if took < 0 {
		took = 0
	}
	files := slices.Clone(r.Listed)
	slices.Sort(files)
	s.trace = traced{took: took, artifacts: r.Artifacts, files: files}
	s.phase = phaseAfter
	return s, Observe{}
}

// nextSample asks for what the next sample needs first: for a job with a job-scoped server, the
// server when none runs, and whether it still runs when a command used it, which the reset then
// precedes; and otherwise the sample's benchmark itself.
func (s State) nextSample() (State, Request) {
	switch {
	case !s.plan.serves():
		return s.runSample()
	case !s.serving:
		return s.serve(phaseSample, false)
	case s.used:
		s.phase = phaseProbe
		return s, ProbeService{}
	}
	return s.runSample()
}

// runSample asks for one run of the benchmark.
func (s State) runSample() (State, Request) {
	s.phase = phaseSample
	s.pending = core.SampleServiceNone
	if s.serving {
		switch {
		case s.used:
			s.pending = core.SampleServiceReused
		case s.restarted:
			s.pending = core.SampleServiceRestarted
		default:
			s.pending = core.SampleServiceStarted
		}
	}
	out := path.Join(s.scratch, fmt.Sprintf("sample-%d.out", len(s.samples)))
	c := s.plan.Benchmark
	r := s.run(StageBenchmark, c, out)
	r.Argv = append(r.Argv, s.plan.Result.OutputFlag(), out)
	return s, r
}

// run is the request to run a command of the task: against the job-scoped server when one runs,
// and against the candidate's server, for a task of two images, whose startup time the limit then
// covers too. The answer to a command against the job-scoped server makes the server used.
func (s State) run(stage Stage, c task.Command, read string) Run {
	r := Run{Stage: stage, Argv: c.Resolve(s.dir), Timeout: c.Timeout, Read: read, OnService: s.serving}
	if cand := s.plan.Candidate; cand != nil {
		serve := cand.ServeAt(s.dir, nil)
		r.Candidate = &core.CandidateRun{Image: s.candidate.ID, Serve: &serve}
		r.Timeout += cand.StartupTimeout
	}
	return r
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
		if why := s.reportedError(r); why != "" {
			if stopped, ok := v.(Stopped); ok {
				stopped.Reason += "; " + why
				v = stopped
			}
		}
		return s.end(v), nil
	}
	got, err := s.read(r)
	if err != nil {
		var reported *reading.Reported
		why := fmt.Sprintf("sample %d: %v", n+1, err)
		if errors.As(err, &reported) {
			why = fmt.Sprintf("sample %d: %v", n+1, reported)
		}
		return s.end(Rejected{Stage: StageBenchmark, Reason: why}), nil
	}
	if missing := s.missingObjective(got); missing != "" {
		return s.end(Rejected{Stage: StageBenchmark, Reason: fmt.Sprintf("sample %d: the benchmark reported no value of the objective %q, and a multi-objective task judges every sample on every objective", n+1, missing)}), nil
	}
	s.samples = append(slices.Clip(s.samples), got.Value)
	if s.plan.serves() {
		s.services = append(slices.Clip(s.services), s.pending)
	}
	for _, o := range got.Others {
		s.others = addObservation(s.others, o)
	}
	if uint32(len(s.samples)) < s.plan.Samples {
		return s.nextSample()
	}
	s.phase = phaseAfter
	return s, Observe{}
}

// reportedError is what a benchmark that did not succeed said about it in its record stream: the
// message of the error record it wrote, or empty when it wrote none or the plan reads no stream. A
// stream that holds a result, or is not one the protocol allows, says nothing about the failure.
func (s State) reportedError(r Ran) string {
	if !s.plan.Result.Protocol2 || !r.Found || r.ReadError != "" {
		return ""
	}
	var reported *reading.Reported
	if _, err := reading.Protocol2(r.Output, s.plan.Metric.Name); errors.As(err, &reported) {
		return reported.Error()
	}
	return ""
}

// missingObjective names an objective of a multi-objective plan that a reading has no value of, and
// is empty when it has every one.
func (s State) missingObjective(got reading.Reading) string {
	for _, m := range s.plan.Objectives.Metrics {
		if m.Name == s.plan.Metric.Name {
			continue
		}
		if !slices.ContainsFunc(got.Others, func(o reading.Named) bool { return o.Name == m.Name }) {
			return m.Name
		}
	}
	return ""
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
	case r.Exec.Kind == core.ExecServiceNotReady && s.plan.Service != nil:
		return stopped(withLog(fmt.Sprintf("the job's server was not ready at %s within %v, in the %s step", s.plan.Service.Ready, s.plan.Service.StartupTimeout, stage), r.ServiceLog))
	case r.Exec.Kind == core.ExecServiceExited && s.plan.Service != nil:
		return stopped(withLog(fmt.Sprintf("the job's server exited during the %s step", stage), r.ServiceLog))
	case r.Exec.Kind == core.ExecServiceNotReady && s.plan.Candidate != nil:
		return stopped(withLog(fmt.Sprintf("the candidate's server was not ready at %s within %v, in the %s step", s.plan.Candidate.Ready, s.plan.Candidate.StartupTimeout, stage), r.ServiceLog))
	case r.Exec.Kind == core.ExecServiceExited && s.plan.Candidate != nil:
		return stopped(withLog(fmt.Sprintf("the candidate's server exited during the %s step", stage), r.ServiceLog))
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

// withLog adds the server's last lines to a reason, when there are any.
func withLog(reason, log string) string {
	if log == "" {
		return reason
	}
	return reason + "; its last lines:\n" + log
}

func (s State) passed(o Observed) State {
	f := Findings{CheckTook: s.checkTook, Machine: s.machine, Load: o.Load, CandidateImage: s.candidate.ID}
	if s.plan.Kind == core.JobTrace {
		f.TraceTook, f.TraceArtifacts, f.TraceFiles = s.trace.took, s.trace.artifacts, slices.Clone(s.trace.files)
	}
	if s.plan.Kind == core.JobMeasure {
		f.Samples = slices.Clone(s.samples)
		f.Observations = s.others
		if s.plan.serves() {
			f.Services = slices.Clone(s.services)
		}
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
	case phaseCandidate:
		return StageCandidate
	case phaseCheck:
		return StageCheck
	case phaseSample:
		return StageBenchmark
	case phaseServe, phaseProbe:
		return StageService
	case phaseReset:
		return StageReset
	case phaseTrace:
		return StageTrace
	}
	return StageObserve
}

func (s State) end(v Verdict) State {
	s.phase = phaseDone
	s.verdict = v
	s.samples, s.others, s.services = nil, nil, nil
	return s
}
