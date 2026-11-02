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
	// PhaseStopping is a running job the daemon could not go on following: its output can no
	// longer be stored, or its supervisor no longer be followed. Its command is being ended, and
	// it holds the machine until the command has.
	PhaseStopping Phase = "stopping"
	// PhaseSettling is a job that held the machine and has ended: its transition is being
	// recorded and its supervisor released. It holds the machine until then, so the store never
	// shows two jobs running and the next job does not start while the last one's end is in doubt.
	PhaseSettling Phase = "settling"
	// PhaseCancelling is a queued job whose cancel is being recorded. It never ran and has no
	// supervisor to wait for, so it does not hold the machine.
	PhaseCancelling Phase = "cancelling"
	// PhaseUnrecorded is a job whose end is known and that its store would not take. The machine
	// keeps the end and offers it again. A command does not run any more, so the job does not hold
	// the machine, but a job that the store has running keeps every other job of that store from
	// starting, so that no store ever shows two.
	PhaseUnrecorded Phase = "unrecorded"
	// PhaseInDoubt is a job the daemon cannot tell the state of: its store is away, or something
	// answers for its supervisor that the daemon cannot use. Its command may run, so it holds the
	// machine, and it is looked at again until the answer is known.
	PhaseInDoubt Phase = "in doubt"
)

type entry struct {
	ref         Ref
	submittedAt time.Time
	seq         uint64
	phase       Phase
	// cancelAsked and cancelReason record a cancel that arrived while the job could not act on it
	// yet. A queued job is cancelled at once, and a running job's cancel is a request to its
	// supervisor.
	cancelAsked  bool
	cancelReason string
	// cancelSent says the request to the supervisor of a running job was made already.
	cancelSent bool
	// settlement and release are the end of a job in PhaseSettling, PhaseCancelling or
	// PhaseUnrecorded: what to offer its store again.
	settlement Settlement
	release    bool
	// trouble says why the job is in PhaseUnrecorded or PhaseInDoubt.
	trouble core.Failure
	// armed says a RetryDue is on its way, and waiting says a step that failed waits for it. A
	// RetryDue with nothing waiting does nothing: it was armed for a phase the job has left, and
	// what it would repeat is in flight.
	armed, waiting bool
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
	// PresenceGone means a run directory exists and its supervisor is known to be dead.
	PresenceGone Presence = "gone"
	// PresenceUnknown means a run directory exists and the daemon cannot tell: something holds the
	// run and does not answer in a way this build can use. Only proof of death makes a run gone.
	PresenceUnknown Presence = "unknown"
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
	// Away says the store of the job could not be read, so State says nothing.
	Away bool
	At   time.Time
}

// CancelAsked says a user asked to cancel a job that the store has as State, which was submitted
// at SubmittedAt. The machine may not have heard of the job yet: a queued job it does not know is
// taken in and cancelled, so that a cancel and a submission can arrive in any order.
type CancelAsked struct {
	Ref         Ref
	Reason      string
	State       core.JobState
	SubmittedAt time.Time
	At          time.Time
}

// Discarded says a run directory left over from an earlier launch is gone, and the queued job
// that had it may start.
type Discarded struct {
	Ref Ref
	At  time.Time
}

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

// FollowFailed says the daemon gave up following a running job: the supervisor would not answer or
// the store would not take the output, for as long as the daemon tried.
type FollowFailed struct {
	Ref     Ref
	Failure core.Failure
	At      time.Time
}

// StopFailed says the daemon could not end a job it stopped following.
type StopFailed struct {
	Ref     Ref
	Failure core.Failure
}

// MoveFailed says the store would not take a job's end, for as long as the daemon tried.
type MoveFailed struct {
	Ref     Ref
	Failure core.Failure
}

// DiscardFailed says what is left of a run could not be released.
type DiscardFailed struct {
	Ref     Ref
	Failure core.Failure
}

// RetryDue says it is time to try again what ArmRetry was asked for.
type RetryDue struct{ Ref Ref }

// RecoveryDone says the machine has been told everything a restart found, so it may start jobs.
type RecoveryDone struct{}

// Closed says the daemon is done with a job: its last transition is recorded, or the store shows
// that it needs nothing more.
type Closed struct{ Ref Ref }

func (Admitted) event()       {}
func (Recovered) event()      {}
func (CancelAsked) event()    {}
func (Discarded) event()      {}
func (Launched) event()       {}
func (LaunchFailed) event()   {}
func (Finished) event()       {}
func (SupervisorLost) event() {}
func (FollowFailed) event()   {}
func (StopFailed) event()     {}
func (MoveFailed) event()     {}
func (DiscardFailed) event()  {}
func (RetryDue) event()       {}
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

// Adopt asks the shell to attach to the job's supervisor, start it if it is still parked, and
// follow it. With CancelFirst, the supervisor is told to cancel before it may start, so a command
// whose cancel arrived in time never runs. The answer is Finished, SupervisorLost or FollowFailed.
type Adopt struct {
	Ref          Ref
	CancelFirst  bool
	CancelReason string
}

// SignalCancel asks the job's supervisor to end the job's process group. There is no answer: the
// outcome of the job says what came of it.
type SignalCancel struct {
	Ref    Ref
	Reason string
}

// Stop asks the shell to end the command of a job it gave up following, and to learn its outcome.
// The answer is Finished, SupervisorLost or StopFailed.
type Stop struct {
	Ref    Ref
	Reason string
}

// ArmRetry asks the shell to answer with RetryDue after a while.
type ArmRetry struct{ Ref Ref }

// Reexamine asks the shell to find out again what the store has of a job and what is left of its
// supervisor. The answer is Recovered.
type Reexamine struct{ Ref Ref }

// Move asks the store to append the settlement's log line to the job's output and to apply its
// transition, and then, when Release is set, to release the job's supervisor and forget its run.
// The answer is Closed, or MoveFailed when the store would not take it.
type Move struct {
	Ref        Ref
	Settlement Settlement
	Release    bool
}

// Discard asks the shell to release what is left of a run: its supervisor when one is alive, and
// its run directory. The answer is DiscardFailed when that did not work. Otherwise it is Discarded
// when Queued says a queued job waits for the run to go, and Closed when none does.
type Discard struct {
	Ref    Ref
	Alive  bool
	Queued bool
}

func (LaunchRequest) request() {}
func (Adopt) request()         {}
func (SignalCancel) request()  {}
func (Stop) request()          {}
func (ArmRetry) request()      {}
func (Reexamine) request()     {}
func (Move) request()          {}
func (Discard) request()       {}

// Step applies one event to the state and returns the new state and the requests the event
// asks for, in the order to carry them out.
//
//   - The machine runs one job at a time, the oldest queued first (by the time it was submitted,
//     then by when the machine heard of it): a free machine and a queued job make a launch.
//   - A cancel of a queued job moves it to cancelled at once. A cancel while a queued job's old run
//     is cleared takes effect when that ends. A cancel during a launch waits for the launch to
//     end: the supervisor is told before it may start, so the command never runs. A cancel of a
//     running job asks its supervisor, once.
//   - An outcome settles the job as Settle says and releases its supervisor. A supervisor that
//     vanished settles it as lost, and a lost job is never retried. The job holds the machine
//     until its settlement is recorded, so the next job starts after it, not beside it.
//   - Recovery is a Recovered event per job, and it follows the same paths: a running job with a
//     live supervisor is adopted, one whose supervisor is dead is lost, a queued job joins the
//     queue whatever is left of an earlier launch, and a run no job needs is discarded. A job or a
//     run the daemon cannot judge, because the store is away or the supervisor gives no usable
//     answer, is touched by nothing: it holds the machine and is looked at again.
//   - The shell tries a store or a supervisor for a bounded time, and then the machine decides. A
//     job it cannot follow is stopped and fails, saying why. A job whose end its store refuses
//     keeps that end and offers it again; it is never closed before the end is recorded.
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
		reqs = out.discarded(e)
	case Launched:
		reqs = out.launched(e.Ref)
	case LaunchFailed:
		reqs = out.launchFailed(e)
	case Finished:
		reqs = out.finished(e)
	case SupervisorLost:
		reqs = out.lost(e)
	case FollowFailed:
		reqs = out.followFailed(e)
	case StopFailed:
		reqs = out.stopFailed(e)
	case MoveFailed:
		reqs = out.moveFailed(e)
	case DiscardFailed:
		reqs = out.discardFailed(e)
	case RetryDue:
		reqs = out.retryDue(e.Ref)
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

// recover applies what a look at a job and its run found. Only a job in doubt is looked at twice:
// any other job the machine holds is already on its way.
func (s *State) recover(e Recovered) []Request {
	var kept entry
	if i := s.find(e.Ref); i >= 0 {
		if s.entries[i].phase != PhaseInDoubt {
			if s.entries[i].phase == PhaseQueued {
				return s.recoverQueued(e)
			}
			return nil
		}
		kept = s.entries[i]
		s.remove(e.Ref)
	}
	alive, unknown := e.Supervisor == PresenceAlive, e.Supervisor == PresenceUnknown
	switch {
	case e.Away:
		if alive || unknown {
			return s.inDoubt(e, kept)
		}
		// Nothing runs for it, and the store can say what it is when it returns.
		return nil
	case e.State == core.JobQueued:
		return s.recoverQueuedAfresh(e, kept)
	case e.State == core.JobRunning:
		switch {
		case alive:
			s.place(e, kept, PhaseRunning)
			i := s.find(e.Ref)
			reqs := []Request{Adopt{Ref: e.Ref}}
			if s.entries[i].cancelAsked {
				s.entries[i].cancelSent = true
				reqs = append(reqs, SignalCancel{Ref: e.Ref, Reason: s.entries[i].cancelReason})
			}
			return reqs
		case unknown:
			return s.inDoubt(e, kept)
		}
		// Its supervisor is dead, so the command ended with it, and the job is lost and never
		// retried.
		s.place(e, kept, PhaseRunning)
		return s.settle(e.Ref, lostJob(e.Ref.Job, e.At))
	default:
		// Final in its store, or not in it at all: a run left over holds nothing of value, once
		// it is known to be what the store says. One the daemon cannot judge is left alone.
		if e.Supervisor == PresenceNone || unknown {
			return nil
		}
		return []Request{Discard{Ref: e.Ref, Alive: alive}}
	}
}

// recoverQueued handles a look at a job that is queued in the machine, which can have a run left.
func (s *State) recoverQueued(e Recovered) []Request {
	if e.Away || e.State != core.JobQueued {
		return nil
	}
	kept := s.entries[s.find(e.Ref)]
	s.remove(e.Ref)
	return s.recoverQueuedAfresh(e, kept)
}

// recoverQueuedAfresh places a job its store has queued. Whatever an earlier launch left is not the
// job: the store never recorded it running, so it never ran. It is released, and the job starts
// over from the queue.
func (s *State) recoverQueuedAfresh(e Recovered, kept entry) []Request {
	switch e.Supervisor {
	case PresenceNone:
		s.place(e, kept, PhaseQueued)
		if s.entries[s.find(e.Ref)].cancelAsked {
			return s.cancelQueuedJob(e.Ref, e.At)
		}
		return nil
	case PresenceUnknown:
		return s.inDoubt(e, kept)
	}
	s.place(e, kept, PhaseDiscarding)
	return []Request{Discard{Ref: e.Ref, Alive: e.Supervisor == PresenceAlive, Queued: true}}
}

// place puts the job in the machine in a phase, keeping what it was told before.
func (s *State) place(e Recovered, kept entry, phase Phase) {
	if kept.seq == 0 {
		s.seq++
		kept = entry{ref: e.Ref, submittedAt: e.SubmittedAt, seq: s.seq}
	}
	kept.phase, kept.waiting = phase, false
	s.entries = append(s.entries, kept)
}

// inDoubt keeps a job the machine cannot judge, and asks for another look.
func (s *State) inDoubt(e Recovered, kept entry) []Request {
	s.place(e, kept, PhaseInDoubt)
	message := "its supervisor gives no answer this build can use"
	if e.Away {
		message = "its store cannot be read"
	}
	i := s.find(e.Ref)
	s.entries[i].trouble = core.Failure{Kind: core.FailureUnavailable, Message: message}
	return s.awaitRetry(i)
}

// awaitRetry has the job at i wait for a RetryDue, arming one unless one is on its way.
func (s *State) awaitRetry(i int) []Request {
	cur := &s.entries[i]
	cur.waiting = true
	if cur.armed {
		return nil
	}
	cur.armed = true
	return []Request{ArmRetry{Ref: cur.ref}}
}

func (s *State) cancel(e CancelAsked) []Request {
	i := s.find(e.Ref)
	if i < 0 {
		if e.State != core.JobQueued {
			return nil
		}
		s.admit(e.Ref, e.SubmittedAt)
		i = s.find(e.Ref)
	}
	cur := &s.entries[i]
	switch cur.phase {
	case PhaseQueued:
		cur.cancelAsked, cur.cancelReason = true, e.Reason
		return s.cancelQueuedJob(e.Ref, e.At)
	case PhaseDiscarding, PhaseLaunching, PhaseInDoubt:
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

// cancelQueuedJob records the cancel of a job that never ran. The job stays in the machine until
// that is recorded, so a store that refuses it does not lose the cancel.
func (s *State) cancelQueuedJob(ref Ref, at time.Time) []Request {
	i := s.find(ref)
	cur := &s.entries[i]
	st := cancelQueued(ref.Job, cur.cancelReason, at)
	cur.phase, cur.settlement, cur.release = PhaseCancelling, st, false
	return []Request{Move{Ref: ref, Settlement: st}}
}

func (s *State) discarded(e Discarded) []Request {
	i := s.find(e.Ref)
	if i < 0 || s.entries[i].phase != PhaseDiscarding {
		return nil
	}
	s.entries[i].phase = PhaseQueued
	if s.entries[i].cancelAsked {
		return s.cancelQueuedJob(e.Ref, e.At)
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
	cur.cancelSent = cur.cancelAsked
	return []Request{Adopt{Ref: ref, CancelFirst: cur.cancelAsked, CancelReason: cur.cancelReason}}
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
	if i < 0 {
		return nil
	}
	switch s.entries[i].phase {
	case PhaseRunning:
		return s.settle(e.Ref, Settle(e.Ref.Job, core.JobRunning, e.Outcome, e.At))
	case PhaseStopping:
		// Whatever the command did, its output did not all reach the store, so the job did not
		// succeed as far as anyone can check.
		return s.settle(e.Ref, stoppedJob(e.Ref.Job, s.entries[i].trouble, e.At))
	}
	return nil
}

func (s *State) lost(e SupervisorLost) []Request {
	i := s.find(e.Ref)
	if i < 0 || (s.entries[i].phase != PhaseRunning && s.entries[i].phase != PhaseStopping) {
		return nil
	}
	return s.settle(e.Ref, lostJob(e.Ref.Job, e.At))
}

func (s *State) followFailed(e FollowFailed) []Request {
	i := s.find(e.Ref)
	if i < 0 || s.entries[i].phase != PhaseRunning {
		return nil
	}
	s.entries[i].phase, s.entries[i].trouble = PhaseStopping, e.Failure
	return []Request{Stop{Ref: e.Ref, Reason: StopReason + e.Failure.Message}}
}

// StopReason starts the reason of a cancel the daemon itself sent, to a job it could not go on
// following. The supervisor keeps that cancel, so a daemon that takes over after a crash finds a
// cancelled command and records the job as cancelled, with this reason, and not as failed.
const StopReason = "the daemon could not follow the job: "

func (s *State) stopFailed(e StopFailed) []Request {
	i := s.find(e.Ref)
	if i < 0 || s.entries[i].phase != PhaseStopping {
		return nil
	}
	s.entries[i].phase, s.entries[i].trouble = PhaseInDoubt, e.Failure
	return s.awaitRetry(i)
}

func (s *State) moveFailed(e MoveFailed) []Request {
	i := s.find(e.Ref)
	if i < 0 {
		return nil
	}
	cur := &s.entries[i]
	switch cur.phase {
	case PhaseSettling, PhaseCancelling, PhaseUnrecorded:
		cur.phase, cur.trouble = PhaseUnrecorded, e.Failure
		return s.awaitRetry(i)
	}
	return nil
}

func (s *State) discardFailed(e DiscardFailed) []Request {
	i := s.find(e.Ref)
	if i < 0 || s.entries[i].phase != PhaseDiscarding {
		return nil
	}
	s.entries[i].trouble = e.Failure
	return s.awaitRetry(i)
}

func (s *State) retryDue(ref Ref) []Request {
	i := s.find(ref)
	if i < 0 {
		return nil
	}
	cur := &s.entries[i]
	due := cur.waiting
	cur.armed, cur.waiting = false, false
	if !due {
		return nil
	}
	switch cur.phase {
	case PhaseUnrecorded:
		return []Request{Move{Ref: ref, Settlement: cur.settlement, Release: cur.release}}
	case PhaseInDoubt:
		return []Request{Reexamine{Ref: ref}}
	case PhaseDiscarding:
		return []Request{Discard{Ref: ref, Alive: true, Queued: true}}
	}
	return nil
}

// settle records a job that held the machine as ended: it keeps holding the machine until the
// shell has recorded the settlement and released the supervisor.
func (s *State) settle(ref Ref, st Settlement) []Request {
	if i := s.find(ref); i >= 0 {
		cur := &s.entries[i]
		cur.phase, cur.settlement, cur.release = PhaseSettling, st, true
	}
	return []Request{Move{Ref: ref, Settlement: st, Release: true}}
}

// schedule starts the oldest queued job when nothing else holds the machine.
func (s *State) schedule() (Request, bool) {
	best := -1
	for i, e := range s.entries {
		if e.phase != PhaseQueued || s.storeBlocked(e.ref.Store) {
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

// busy reports whether a job holds the machine: one whose command may run or whose end is on its
// way to the store.
func (s *State) busy() bool {
	return slices.ContainsFunc(s.entries, func(e entry) bool {
		switch e.phase {
		case PhaseQueued, PhaseCancelling, PhaseUnrecorded:
			return false
		}
		return true
	})
}

// storeBlocked reports whether the store still shows a job running that has ended.
func (s *State) storeBlocked(id StoreID) bool {
	return slices.ContainsFunc(s.entries, func(e entry) bool {
		return e.ref.Store == id && e.phase == PhaseUnrecorded && e.settlement.Transition.From == core.JobRunning
	})
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

// Trouble is a job that cannot go on until something outside the daemon does: why, and which job.
type Trouble struct {
	Ref     Ref
	Failure core.Failure
}

// Troubled returns the jobs in PhaseUnrecorded and PhaseInDoubt. Whoever waits for one of them has
// no end to wait for until the trouble passes, and is told so.
func (s State) Troubled() []Trouble {
	var out []Trouble
	for _, e := range s.entries {
		if e.phase == PhaseUnrecorded || e.phase == PhaseInDoubt {
			out = append(out, Trouble{Ref: e.ref, Failure: e.trouble})
		}
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
// for a free machine and nothing holds the machine, and the job's store does not hold it back.
// Every other job the machine holds waits for an answer the shell owes it (a launch, a discard, a
// supervisor's outcome, a retry), so a state with no stranded job has no orphan wait.
func (s State) Stranded() []Ref {
	if s.busy() || s.recovering {
		return nil
	}
	var out []Ref
	for _, e := range s.entries {
		if e.phase == PhaseQueued && !s.storeBlocked(e.ref.Store) {
			out = append(out, e.ref)
		}
	}
	return out
}
