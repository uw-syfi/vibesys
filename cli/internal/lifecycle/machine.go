package lifecycle

import (
	"slices"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// Phase is where the machine has a job.
type Phase string

// The phases. The machine keeps a job only while it needs the daemon's attention: a job that is
// final, or that the daemon has given up on, is not in it.
const (
	// PhaseQueued is a job waiting for the machine to be free.
	PhaseQueued Phase = "queued"
	// PhaseDiscarding is a queued job whose leftover supervisor from an earlier daemon is being
	// released. It waits for that to end, so a new launch does not meet the old run.
	PhaseDiscarding Phase = "discarding"
	// PhaseLaunching is the one job a supervisor is being started for. The store still has it
	// queued until the launch records it as running.
	PhaseLaunching Phase = "launching"
	// PhaseRunning is a job the store has running, with a supervisor to report on it.
	PhaseRunning Phase = "running"
	// PhaseSettling is a job that held the machine and has ended: its transition is being
	// recorded and its supervisor released. It holds the machine until then, so the store never
	// shows two jobs running and the next job does not start while the last one's end is in doubt.
	PhaseSettling Phase = "settling"
)

type entry struct {
	ref         Ref
	submittedAt time.Time
	seq         uint64
	phase       Phase
	// cancelAsked and cancelReason record a cancel that arrived while the job was launching. A
	// queued job is cancelled at once, and a running job's cancel is a request to its supervisor.
	cancelAsked  bool
	cancelReason string
	// cancelSent says the request to the supervisor of a running job was made already.
	cancelSent bool
}

// State is the machine's whole memory: the jobs that need the daemon, and the order they came in.
// It is a value: Step returns a new State and leaves the old one as it was.
type State struct {
	entries []entry
	seq     uint64
	// recovering holds every launch back until RecoveryDone, so that a queued job is not started
	// before the machine has heard of the job that is still running.
	recovering bool
}

// Recovering returns the state of a daemon that is starting: it launches nothing until it has
// been told of every job and run left from before, and then RecoveryDone. A first start is the
// same with nothing to be told.
func Recovering() State { return State{recovering: true} }

// Presence says what a restarted daemon found of a job's supervisor.
type Presence string

// The presences.
const (
	// PresenceNone means no run directory exists for the job.
	PresenceNone Presence = "none"
	// PresenceAlive means a supervisor answered, parked or running or finished and waiting to be
	// released.
	PresenceAlive Presence = "alive"
	// PresenceGone means a run directory exists and no supervisor answers.
	PresenceGone Presence = "gone"
)

// Event is something that happened, as the machine is told. Every event carries the time it
// happened, which the machine never reads from anywhere else. The set is closed.
type Event interface {
	event()
}

// Admitted says a job is queued in its store and wants to run. A submission produces one. Telling
// the machine of a job it has already is harmless.
type Admitted struct {
	Ref         Ref
	SubmittedAt time.Time
}

// Recovered says what a restarted daemon found of one job: the state its store has (empty when
// the store has no such job) and what is left of its supervisor. Startup feeds one per job and per
// run directory before it takes a request, and a first start is the same with nothing to feed.
type Recovered struct {
	Ref         Ref
	State       core.JobState
	SubmittedAt time.Time
	Supervisor  Presence
	At          time.Time
}

// CancelAsked says a user asked to cancel a job.
type CancelAsked struct {
	Ref    Ref
	Reason string
	At     time.Time
}

// Discarded says a run directory left over from an earlier launch is gone, and the queued job
// that had it may start.
type Discarded struct{ Ref Ref }

// Launched says the job's supervisor is up and the store has the job running.
type Launched struct{ Ref Ref }

// LaunchFailed says no supervisor could be started for the job, and the store still has it queued.
type LaunchFailed struct {
	Ref     Ref
	Failure core.Failure
	At      time.Time
}

// Finished says the job's supervisor reported its outcome.
type Finished struct {
	Ref     Ref
	Outcome Outcome
	At      time.Time
}

// SupervisorLost says the job's supervisor went away without an outcome.
type SupervisorLost struct {
	Ref Ref
	At  time.Time
}

// RecoveryDone says the machine has been told everything a restart found, so it may start jobs.
type RecoveryDone struct{}

// Closed says the daemon is done with a job: its last transition is recorded, or the daemon gave
// up on recording it and the next start will recover it.
type Closed struct{ Ref Ref }

func (Admitted) event()       {}
func (Recovered) event()      {}
func (CancelAsked) event()    {}
func (Discarded) event()      {}
func (Launched) event()       {}
func (LaunchFailed) event()   {}
func (Finished) event()       {}
func (SupervisorLost) event() {}
func (RecoveryDone) event()   {}
func (Closed) event()         {}

// Request is something the machine asks the shell to do. Each names the job it is about, and the
// shell answers it with an event. The set is closed.
type Request interface {
	request()
}

// LaunchRequest asks for a supervisor for the job, the store to record it running, and the
// supervisor to start. The answer is Launched or LaunchFailed.
type LaunchRequest struct{ Ref Ref }

// Adopt asks the shell to attach to the job's live supervisor, start it if it is still parked,
// and follow it. The answer is Finished or SupervisorLost.
type Adopt struct{ Ref Ref }

// SignalCancel asks the job's supervisor to end the job's process group.
type SignalCancel struct {
	Ref    Ref
	Reason string
}

// Move asks the store to apply the settlement's transition, to append its log line to the job's
// output, and, when Release is set, to release the job's supervisor and forget its run. The
// answer is Closed.
type Move struct {
	Ref        Ref
	Settlement Settlement
	Release    bool
}

// Discard asks the shell to release what is left of a run: its supervisor when one is alive, and
// its run directory. The answer is Discarded for a job that is queued and Closed for any other.
type Discard struct {
	Ref   Ref
	Alive bool
}

func (LaunchRequest) request() {}
func (Adopt) request()         {}
func (SignalCancel) request()  {}
func (Move) request()          {}
func (Discard) request()       {}

// Step applies one event to the state and returns the new state and the requests the event
// asks for, in the order to carry them out.
//
//   - The machine runs one job at a time, the oldest queued first (by the time it was submitted,
//     then by when the machine heard of it): a free machine and a queued job make a launch.
//   - A cancel of a queued job moves it to cancelled at once. A cancel during a launch waits for
//     the launch to end, and then cancels the job it started or, if the launch failed, cancels
//     the job that never ran. A cancel of a running job asks its supervisor, once.
//   - An outcome settles the job as Settle says and releases its supervisor. A supervisor that
//     vanished settles it as lost, and a lost job is never retried. The job holds the machine
//     until its settlement is recorded, so the next job starts after it, not beside it.
//   - Recovery is a Recovered event per job, and it follows the same paths: a running job with a
//     live supervisor is adopted, one without is lost, a queued job joins the queue whatever is
//     left of an earlier launch, and a run no job needs is discarded.
//
// A job the machine does not hold ignores a cancel, an outcome and a loss, so a duplicate or late
// event changes nothing.
func Step(s State, e Event) (State, []Request) {
	out := State{entries: slices.Clone(s.entries), seq: s.seq, recovering: s.recovering}
	var reqs []Request
	switch e := e.(type) {
	case Admitted:
		out.admit(e.Ref, e.SubmittedAt)
	case Recovered:
		reqs = out.recover(e)
	case CancelAsked:
		reqs = out.cancel(e)
	case Discarded:
		if i := out.find(e.Ref); i >= 0 && out.entries[i].phase == PhaseDiscarding {
			out.entries[i].phase = PhaseQueued
		}
	case Launched:
		reqs = out.launched(e.Ref)
	case LaunchFailed:
		reqs = out.launchFailed(e)
	case Finished:
		reqs = out.finished(e)
	case SupervisorLost:
		reqs = out.lost(e)
	case RecoveryDone:
		out.recovering = false
	case Closed:
		out.remove(e.Ref)
	}
	if r, ok := out.schedule(); ok {
		reqs = append(reqs, r)
	}
	return out, reqs
}

func (s *State) find(ref Ref) int {
	return slices.IndexFunc(s.entries, func(e entry) bool { return e.ref == ref })
}

func (s *State) remove(ref Ref) {
	if i := s.find(ref); i >= 0 {
		s.entries = slices.Delete(s.entries, i, i+1)
	}
}

func (s *State) admit(ref Ref, at time.Time) {
	if s.find(ref) >= 0 {
		return
	}
	s.seq++
	s.entries = append(s.entries, entry{ref: ref, submittedAt: at, seq: s.seq, phase: PhaseQueued})
}

func (s *State) recover(e Recovered) []Request {
	alive := e.Supervisor == PresenceAlive
	switch e.State {
	case core.JobQueued:
		// Whatever an earlier launch left is not the job: the store never recorded it running, so
		// it never ran. It is released, and the job starts over from the queue.
		s.admit(e.Ref, e.SubmittedAt)
		if e.Supervisor == PresenceNone {
			return nil
		}
		if i := s.find(e.Ref); i >= 0 && s.entries[i].phase == PhaseQueued {
			s.entries[i].phase = PhaseDiscarding
		}
		return []Request{Discard{Ref: e.Ref, Alive: alive}}
	case core.JobRunning:
		if i := s.find(e.Ref); i >= 0 {
			return nil
		}
		s.seq++
		s.entries = append(s.entries, entry{ref: e.Ref, submittedAt: e.SubmittedAt, seq: s.seq, phase: PhaseRunning})
		if alive {
			return []Request{Adopt{Ref: e.Ref}}
		}
		// Its supervisor is gone, so the command was ended with it or is out of reach, and either
		// way the job is lost and never retried.
		return s.settle(e.Ref, lostJob(e.Ref.Job, e.At))
	default:
		// Final in its store, or not in it at all: a run left over is garbage.
		if e.Supervisor == PresenceNone {
			return nil
		}
		return []Request{Discard{Ref: e.Ref, Alive: alive}}
	}
}

func (s *State) cancel(e CancelAsked) []Request {
	i := s.find(e.Ref)
	if i < 0 {
		return nil
	}
	cur := &s.entries[i]
	switch cur.phase {
	case PhaseQueued:
		s.remove(e.Ref)
		return []Request{Move{Ref: e.Ref, Settlement: cancelQueued(e.Ref.Job, e.Reason, e.At)}}
	case PhaseLaunching:
		if !cur.cancelAsked {
			cur.cancelAsked, cur.cancelReason = true, e.Reason
		}
	case PhaseRunning:
		if !cur.cancelAsked {
			cur.cancelAsked, cur.cancelReason = true, e.Reason
		}
		if !cur.cancelSent {
			cur.cancelSent = true
			return []Request{SignalCancel{Ref: e.Ref, Reason: cur.cancelReason}}
		}
	}
	return nil
}

func (s *State) launched(ref Ref) []Request {
	i := s.find(ref)
	if i < 0 || s.entries[i].phase != PhaseLaunching {
		return nil
	}
	cur := &s.entries[i]
	cur.phase = PhaseRunning
	if cur.cancelAsked {
		cur.cancelSent = true
		return []Request{SignalCancel{Ref: ref, Reason: cur.cancelReason}}
	}
	return nil
}

func (s *State) launchFailed(e LaunchFailed) []Request {
	i := s.find(e.Ref)
	if i < 0 || s.entries[i].phase != PhaseLaunching {
		return nil
	}
	cur := s.entries[i]
	if cur.cancelAsked {
		// The job never ran, so the cancel the user asked for is the end it gets.
		return s.settle(e.Ref, cancelQueued(e.Ref.Job, cur.cancelReason, e.At))
	}
	return s.settle(e.Ref, failedToLaunch(e.Ref.Job, e.Failure, e.At))
}

func (s *State) finished(e Finished) []Request {
	i := s.find(e.Ref)
	if i < 0 || s.entries[i].phase != PhaseRunning {
		return nil
	}
	return s.settle(e.Ref, Settle(e.Ref.Job, core.JobRunning, e.Outcome, e.At))
}

func (s *State) lost(e SupervisorLost) []Request {
	i := s.find(e.Ref)
	if i < 0 || s.entries[i].phase != PhaseRunning {
		return nil
	}
	return s.settle(e.Ref, lostJob(e.Ref.Job, e.At))
}

// settle records a job that held the machine as ended: it keeps holding the machine until the
// shell has recorded the settlement and released the supervisor.
func (s *State) settle(ref Ref, st Settlement) []Request {
	if i := s.find(ref); i >= 0 {
		s.entries[i].phase = PhaseSettling
	}
	return []Request{Move{Ref: ref, Settlement: st, Release: true}}
}

// schedule starts the oldest queued job when nothing else holds the machine.
func (s *State) schedule() (Request, bool) {
	best := -1
	for i, e := range s.entries {
		if e.phase != PhaseQueued {
			continue
		}
		if best < 0 || e.submittedAt.Before(s.entries[best].submittedAt) ||
			(e.submittedAt.Equal(s.entries[best].submittedAt) && e.seq < s.entries[best].seq) {
			best = i
		}
	}
	if best < 0 || s.busy() || s.recovering {
		return nil, false
	}
	s.entries[best].phase = PhaseLaunching
	return LaunchRequest{Ref: s.entries[best].ref}, true
}

// busy reports whether a job holds the machine: one being launched, running or settling.
func (s *State) busy() bool {
	return slices.ContainsFunc(s.entries, func(e entry) bool { return e.phase != PhaseQueued })
}

// Held is one job the machine holds.
type Held struct {
	Ref         Ref
	Phase       Phase
	CancelAsked bool
}

// Held returns the jobs the machine holds, in the order it heard of them.
func (s State) Held() []Held {
	out := make([]Held, 0, len(s.entries))
	for _, e := range s.entries {
		out = append(out, Held{Ref: e.ref, Phase: e.phase, CancelAsked: e.cancelAsked})
	}
	return out
}

// Counts returns how many jobs wait for the machine and how many hold it. A job being launched or
// discarded is counted as waiting, since its store still has it queued.
func (s State) Counts() (queued, running int) {
	for _, e := range s.entries {
		if e.phase == PhaseRunning {
			running++
		} else {
			queued++
		}
	}
	return queued, running
}

// Stranded returns the queued jobs that nothing will ever start: it is empty unless a job waits
// for a free machine and nothing holds the machine. Every other job the machine holds waits for
// an answer the shell owes it (a launch, a discard, a supervisor's outcome), so a state with no
// stranded job has no orphan wait.
func (s State) Stranded() []Ref {
	if s.busy() || s.recovering {
		return nil
	}
	var out []Ref
	for _, e := range s.entries {
		if e.phase == PhaseQueued {
			out = append(out, e.ref)
		}
	}
	return out
}
