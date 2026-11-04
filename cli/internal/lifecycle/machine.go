package lifecycle

import (
	"fmt"
	"slices"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/schedule"
)

// Phase is where the machine has a job.
type Phase string

// The phases. The machine keeps a job only while it needs the daemon's attention: a job that is
// final, or that the daemon has given up on, is not in it.
const (
	// PhaseQueued is a job waiting for the machine to be free.
	PhaseQueued Phase = "queued"
	// PhaseDiscarding is a queued job whose leftover supervisor from an earlier daemon is being
	// released. It waits for that to end, so a new launch does not meet the old run. Nothing of it
	// runs, as a job the store has queued never started, so it does not hold the machine.
	PhaseDiscarding Phase = "discarding"
	// PhaseLaunching is the one job a supervisor is being started for. The store still has it
	// queued until the launch records it as running.
	PhaseLaunching Phase = "launching"
	// PhaseRunning is a job the store has running, with a supervisor to report on it.
	PhaseRunning Phase = "running"
	// PhaseStopping is a running job the daemon could not go on following: its store refuses its
	// output, or its supervisor no longer answers. Its command is being ended, and it holds the
	// machine until the command has. A store that is only away never brings a job here.
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
	// PhaseInDoubt is a job the daemon cannot tell the state of: its store is away, or what is left
	// of its supervisor cannot be judged. It is looked at again until the answer is known. It
	// always holds back the other jobs of its store, which might otherwise show two running. It
	// holds the machine only while its command can run, and for a bounded time: see entry.holds.
	PhaseInDoubt Phase = "in doubt"
	// PhaseLeft is a job of a store the user gave up for good, whose command the shell could not
	// end. Nothing records it any more, and it holds back no store. It holds the machine while its
	// command can run, and no longer than the command's limit, so the next job does not start
	// beside it; then it leaves the machine.
	PhaseLeft Phase = "left"
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
	// holds says a job in doubt holds the machine, and holdUntil is when it stops, whatever it
	// looks like then: a command cannot outlive its time limit. A job in doubt with no look yet has
	// a zero holdUntil, and the look it is owed sets it.
	holds     bool
	holdUntil time.Time
	// parked says a job in doubt is known: its store has it running and its command has not
	// started. It starts in its turn.
	parked bool
	// attempts is how many attempts at the job were cut off, as its store keeps them, and policy
	// when it may run again after its supervisor is proved gone. spawnFailures is how many launches
	// of it failed for want of a supervisor in this daemon's life, which its store does not keep.
	attempts      int
	policy        core.RerunPolicy
	spawnFailures int
	// work is where the job may run and what it claims there, as its store says. A job the machine
	// was never told the work of claims the whole machine.
	work Work
	// placed is the option the scheduler started the job on, which it holds until its end is
	// recorded. It is nil for a job the scheduler has not started since it was last queued.
	placed *schedule.Option
	// wait is why the scheduler did not start the job at the last step, when it is queued or parked
	// and did not start, and refused says its claims can never be granted.
	wait    schedule.WaitReason
	waits   bool
	refused string
	// leased is the lease another holder had when the job last tried to take it. The job waits
	// for a RetryDue to try again.
	leased *schedule.LeasedOut
}

// Work is what a job asks of the machine, as the shell read it from the job: the places it may run,
// each with the claims it holds there from its launch until its end is recorded (a job that
// started may run only where it started), the run its use is counted for, and its kind, which says
// what it is to whoever reads why another job waits. Work with no options and no kind is work the
// machine was not told, and claims every resource for the job alone; told work with no options may
// run nowhere, and the scheduler refuses it.
type Work struct {
	Options []schedule.Option
	Run     core.RunID
	Kind    core.JobKind
}

func (w Work) told() bool { return len(w.Options) > 0 || w.Kind != "" }

// Scheduling is how the machine is shared: the resources jobs claim, the policy that orders the
// jobs that wait, the placer that chooses where each runs, and the filters that may hold one back.
// A nil policy is arrival order, and a nil placer schedule.LeastLoaded.
type Scheduling struct {
	Resources []schedule.Resource
	Policy    schedule.Policy
	Placer    schedule.Placer
	Filters   []schedule.Filter
}

// OneAtATime is the scheduling of a daemon whose config says nothing: one slot of
// schedule.DefaultPlacement, and the oldest job first.
func OneAtATime() Scheduling {
	return Scheduling{Resources: []schedule.Resource{{ID: schedule.DefaultPlacement, Capacity: 1}}}
}

// awayStore is a store the daemon could not read, whose jobs it has not all heard of.
type awayStore struct {
	id      StoreID
	failure core.Failure
}

// State is the machine's whole memory: the jobs that need the daemon, and the order they came in.
// It is a value: Step returns a new State and leaves the old one as it was.
type State struct {
	entries []entry
	seq     uint64
	// recovering holds every launch back until RecoveryDone, so that a queued job is not started
	// before the machine has heard of the job that is still running.
	recovering bool
	// unread is the stores whose unfinished jobs the machine has not read. A job of such a store
	// could be running in it, so none of its jobs starts until it is read. Other stores go on.
	unread []awayStore
	// scheduling is how the machine is shared, and recent the last starts it granted, which a fair
	// policy shares the machine by.
	scheduling Scheduling
	recent     []schedule.Grant
}

// Recovering returns the state of a daemon that is starting: it launches nothing until it has
// been told of every job and run left from before, and then RecoveryDone. A first start is the
// same with nothing to be told. It runs one job at a time (OneAtATime).
func Recovering() State { return RecoveringWith(OneAtATime()) }

// RecoveringWith is Recovering, with the machine shared as the scheduling says.
func RecoveringWith(sc Scheduling) State {
	sc.Resources = slices.Clone(sc.Resources)
	sc.Filters = slices.Clone(sc.Filters)
	return State{recovering: true, scheduling: sc}
}

// RunPhase is where a supervisor is in its life, as it says itself.
type RunPhase string

// The phases of a supervisor. Only RunRunning has a command that can be using the machine.
const (
	// RunParked is a supervisor that has not started the command, and will not until it is told.
	RunParked RunPhase = "parked"
	// RunRunning is a supervisor whose command is running.
	RunRunning RunPhase = "running"
	// RunFinished is a supervisor whose command ended. It keeps the outcome and the output until it
	// is released, however long that takes.
	RunFinished RunPhase = "finished"
	// RunReleased is a supervisor that was told it may go.
	RunReleased RunPhase = "released"
)

// DeathProof is the evidence that a supervisor is gone. It is the only thing that makes a job lost
// or lets a run be forgotten: no answer, a nil handle and a missing directory are none of these.
// The zero value proves nothing.
type DeathProof string

// The proofs.
const (
	// ProofLockFree is a supervisor's lock that could be taken, which the system frees when the
	// process ends however it ends.
	ProofLockFree DeathProof = "its lock is free"
	// ProofRebooted is a machine that started again since the run began.
	ProofRebooted DeathProof = "the machine restarted"
	// ProofReleased is a supervisor that was told to go and answered.
	ProofReleased DeathProof = "it was released"
)

// Valid says the proof is one.
func (p DeathProof) Valid() bool {
	return p == ProofLockFree || p == ProofRebooted || p == ProofReleased
}

// Presence is what was found of a job's supervisor. The set is closed.
type Presence interface {
	presence()
}

// RunNone says there is no sign of a run for the job. It says nothing of a run that lost its traces,
// so it is no proof that a job the store has running ended.
type RunNone struct{}

// RunAlive says a supervisor answered. Limit is the longest its command can run.
type RunAlive struct {
	Phase RunPhase
	Limit time.Duration
}

// RunDead says a supervisor is gone, Proof says how that is known, and Start what is known of
// whether its command was told to start.
type RunDead struct {
	Proof DeathProof
	Start Start
}

// Start is what is known of whether a run's command was told to start. The set is closed, and a
// nil Start says no more than MayHaveStarted: only NeverStarted proves anything.
type Start interface {
	start()
}

// NeverStarted says the run's command was never told to start, so nothing of it ran. Its record
// says so, and the daemon writes that down before it tells a supervisor to start.
type NeverStarted struct{}

// MayHaveStarted says the run's command may have started.
type MayHaveStarted struct{}

func (NeverStarted) start()   {}
func (MayHaveStarted) start() {}

// RunUnknown says something may live that the daemon cannot use. Limit is the longest a command of
// it can run.
type RunUnknown struct{ Limit time.Duration }

func (RunNone) presence()    {}
func (RunAlive) presence()   {}
func (RunDead) presence()    {}
func (RunUnknown) presence() {}

// Finding is what the store said of a job. The set is closed.
type Finding interface {
	finding()
}

// StoreAway says the store could not be read, so nothing is known of the job.
type StoreAway struct{}

// NoSuchJob says the store has no such job, or none that needs the daemon.
type NoSuchJob struct{}

// JobIn says the store has the job, in State. GivenUp says the store has the job running and was
// taken over after its owner record was removed, which is how a user says that the place that ran
// the job is gone for good: then a run of it that cannot be found or used from here is no run.
// Attempts is how many attempts at the job were cut off, Policy is the job's rerun policy, and Work
// what it claims.
type JobIn struct {
	State       core.JobState
	SubmittedAt time.Time
	GivenUp     bool
	Attempts    int
	Policy      core.RerunPolicy
	Work        Work
}

func (StoreAway) finding() {}
func (NoSuchJob) finding() {}
func (JobIn) finding()     {}

// Cause is who a failed step depends on, which decides what the machine does next. The set is
// closed.
type Cause string

// The causes.
const (
	// CauseStoreAway is a store that cannot be reached or is not this daemon's now. It says nothing
	// about the job, so a running command goes on, and its supervisor keeps the output.
	CauseStoreAway Cause = "store away"
	// CauseSupervisorSilent is a supervisor that does not answer.
	CauseSupervisorSilent Cause = "supervisor silent"
	// CauseRefused is an answer that will not change, such as an environment that does not exist.
	CauseRefused Cause = "refused"
	// CauseUnprepared is a launch that failed before it started a supervisor, such as one whose
	// environment gave no fingerprint. Nothing ran, and nothing says a second try goes further.
	CauseUnprepared Cause = "unprepared"
)

// Event is something that happened, as the machine is told. Every event carries the time it
// happened, which the machine never reads from anywhere else. The set is closed.
type Event interface {
	event()
}

// Admitted says a job is queued in its store and wants to run. A submission produces one. Telling
// the machine of a job it has already is harmless. Attempts is how many attempts at the job were
// cut off, Policy is the job's rerun policy, and Work what it claims.
type Admitted struct {
	Ref         Ref
	SubmittedAt time.Time
	Attempts    int
	Policy      core.RerunPolicy
	Work        Work
}

// Recovered says what a look at one job found: what its store says and what is left of its
// supervisor. Startup feeds one per job and per run before it takes a request, and a first start is
// the same with nothing to feed.
type Recovered struct {
	Ref        Ref
	Finding    Finding
	Supervisor Presence
	At         time.Time
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
	Cause   Cause
	Failure core.Failure
	At      time.Time
}

// Finished says the job's supervisor reported its outcome.
type Finished struct {
	Ref     Ref
	Outcome Outcome
	At      time.Time
}

// SupervisorLost says the job's supervisor went away without an outcome, Proof says how that is
// known, and Start what is known of whether its command was told to start.
type SupervisorLost struct {
	Ref   Ref
	Proof DeathProof
	Start Start
	At    time.Time
}

// FollowFailed says the daemon gave up following a running job for now: the supervisor would not
// answer or the store would not take the output, for as long as the daemon tried.
type FollowFailed struct {
	Ref     Ref
	Cause   Cause
	Failure core.Failure
	At      time.Time
}

// CancelFailed says a request to cancel did not reach the job's supervisor.
type CancelFailed struct{ Ref Ref }

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

// LeaseRefused says a launch took nothing and started nothing, because another holder has the lease
// of one of the job's resources, another daemon or another job of this one, or because the place
// the lease is kept could not be reached, which Detail says.
type LeaseRefused struct {
	Ref      Ref
	Resource schedule.ResourceID
	Holder   core.LeaseHolder
	Detail   string
	At       time.Time
}

// RetryDue says it is time to try again what ArmRetry was asked for.
type RetryDue struct{ Ref Ref }

// RecoveryDone says the machine has been told everything a restart found, so it may start jobs.
type RecoveryDone struct{}

// StoreUnread says a store could not be read, so its jobs are not all known.
type StoreUnread struct {
	Store   StoreID
	Failure core.Failure
}

// StoreRead says a store was read and the machine was told what it holds.
type StoreRead struct{ Store StoreID }

// StoreForgotten says the user gave a store up for good: the machine forgets its jobs. The shell
// sends it once it has tried to end their runs, and Left is what it found, at At, of each run it
// could not end: a job whose command may still run holds the machine until its limit passes.
type StoreForgotten struct {
	Store StoreID
	Left  []LeftRun
	At    time.Time
}

// LeftRun is a run the shell could not end, and what was found of it.
type LeftRun struct {
	Ref        Ref
	Supervisor Presence
}

// Closed says the daemon is done with a job: its last transition is recorded, or the store shows
// that it needs nothing more.
type Closed struct{ Ref Ref }

// Requeued says the store took a settlement that queues the job again (see Settlement.Requeues):
// the attempt that was cut off is recorded, and the job waits for its turn.
type Requeued struct{ Ref Ref }

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
func (CancelFailed) event()   {}
func (RetryDue) event()       {}
func (LeaseRefused) event()   {}
func (RecoveryDone) event()   {}
func (StoreUnread) event()    {}
func (StoreRead) event()      {}
func (StoreForgotten) event() {}
func (Closed) event()         {}
func (Requeued) event()       {}

// Request is something the machine asks the shell to do. Each names the job it is about, and the
// shell answers it with an event. The set is closed.
type Request interface {
	// Target is the job the request is for.
	Target() Ref
	request()
}

// LaunchRequest asks for a supervisor for the job, the store to record it running, and the
// supervisor to start. The answer is Launched or LaunchFailed. Placement is where the scheduler
// started it: the placement its docker job runs on, empty for one that runs on none. Leases are the
// resources the job holds alone there: before anything else the shell takes the lease of each that
// other daemons share, and answers LeaseRefused, having started nothing, when another holds one.
type LaunchRequest struct {
	Ref       Ref
	Placement string
	Leases    []schedule.ResourceID
}

// Adopt asks the shell to attach to the job's supervisor, start it if it is still parked, and
// follow it. With CancelFirst, the supervisor is told to cancel before it may start, so a command
// whose cancel arrived in time never runs. The answer is Finished, SupervisorLost or FollowFailed.
// Leases are the resources the job holds alone, as for LaunchRequest: the shell keeps their leases
// for the job, taking any it does not hold yet, such as after a restart. A lease another holds by
// then does not stop a job that was already started; the shell reports the conflict.
type Adopt struct {
	Ref          Ref
	CancelFirst  bool
	CancelReason string
	Leases       []schedule.ResourceID
}

// SignalCancel asks the job's supervisor to end the job's process group. The outcome of the job
// says what came of it, and the answer is CancelFailed when the request did not arrive.
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
// The answer is Closed, Requeued for a settlement that queues the job again, or MoveFailed when the
// store would not take it.
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
	Queued bool
}

// Target implements Request.
func (r LaunchRequest) Target() Ref { return r.Ref }

// Target implements Request.
func (r Adopt) Target() Ref { return r.Ref }

// Target implements Request.
func (r SignalCancel) Target() Ref { return r.Ref }

// Target implements Request.
func (r Stop) Target() Ref { return r.Ref }

// Target implements Request.
func (r ArmRetry) Target() Ref { return r.Ref }

// Target implements Request.
func (r Reexamine) Target() Ref { return r.Ref }

// Target implements Request.
func (r Move) Target() Ref { return r.Ref }

// Target implements Request.
func (r Discard) Target() Ref { return r.Ref }

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
//   - The machine starts every job the scheduler admits (internal/schedule): each job claims
//     resources from its launch until its settlement is recorded, no resource is given past its
//     capacity, an exclusive claim shares its resource with nothing, and the policy picks among
//     the rest. A job with a parked supervisor goes before every queued one. With OneAtATime, the
//     default, the machine runs one job at a time, the oldest queued first (by the time it was
//     submitted, then by when the machine heard of it). A job whose claims can never be granted
//     stays queued, and waits on that as on trouble.
//   - A cancel of a queued job moves it to cancelled at once. A cancel while a queued job's old run
//     is cleared takes effect when that ends. A cancel during a launch waits for the launch to
//     end: the supervisor is told before it may start, so the command never runs. A cancel of a
//     running job asks its supervisor, once.
//   - An outcome settles the job as Settle says and releases its supervisor. The job holds its
//     claims until its settlement is recorded, so the next job that needs them starts after it,
//     not beside it.
//   - A supervisor proved gone cuts the job's attempt off, as CutOff says: the job is queued again,
//     in its old place, when its command was never told to start or its kind is rerun after any
//     proven death, and while it has had fewer than core.MaxAttempts; otherwise it is lost. The
//     dead run is released before the job is launched again. A job the user asked to cancel, or
//     that the daemon was stopping, is never queued again, and neither is one with an outcome.
//   - A launch that failed for want of a supervisor, before the store had the job running, is
//     tried again up to core.MaxAttempts times in the daemon's life, and then fails the job.
//   - Recovery is a Recovered event per job, and it follows the same paths: a running job with a
//     live supervisor is adopted, one whose supervisor is proved dead is cut off, a queued job joins
//     the queue whatever is left of an earlier launch, and a run no job needs is discarded. A job
//     or a run the daemon cannot judge is touched by nothing and looked at again.
//   - One repository's trouble is its own. A job in doubt holds back the jobs of its store, and it
//     holds its claims only while a command of it can run: a supervisor that is parked, finished
//     or dead holds nothing, and one that runs or cannot be judged holds them for at most the time
//     limit of its command. A job whose work the machine was never told claims every resource
//     alone. A store that was not read starts none of its jobs.
//   - A store that is away never ends a job. A running job goes in doubt and is adopted where it
//     left off when the store answers, as its supervisor keeps the output, and a launch that
//     failed on it goes back to the queue the same way. A supervisor that does not answer, or a
//     store that refuses for good, stops the job, which fails saying why. A job whose end its store
//     does not take keeps that end and offers it again; it is never closed before it is recorded.
//   - A job is lost only with a DeathProof.
//   - A store the user gave up leaves the machine with its jobs once the shell has tried to end
//     their runs. A run the shell could not end holds the machine while its command can run, for
//     at most its limit, as a job in doubt does.
//
// A job the machine does not hold ignores a cancel, an outcome and a loss, so a duplicate or late
// event changes nothing.
func Step(s State, e Event) (State, []Request) {
	out := State{
		entries: slices.Clone(s.entries), seq: s.seq, recovering: s.recovering, unread: slices.Clone(s.unread),
		scheduling: s.scheduling, recent: s.recent,
	}
	var reqs []Request
	switch e := e.(type) {
	case Admitted:
		out.admit(e)
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
	case LeaseRefused:
		reqs = out.leaseRefused(e)
	case Finished:
		reqs = out.finished(e)
	case SupervisorLost:
		reqs = out.lost(e)
	case FollowFailed:
		reqs = out.followFailed(e)
	case StopFailed:
		reqs = out.stopFailed(e)
	case CancelFailed:
		reqs = out.cancelFailed(e.Ref)
	case MoveFailed:
		reqs = out.moveFailed(e)
	case DiscardFailed:
		reqs = out.discardFailed(e)
	case RetryDue:
		reqs = out.retryDue(e.Ref)
	case RecoveryDone:
		out.recovering = false
	case StoreUnread:
		if !out.isUnread(e.Store) {
			out.unread = append(out.unread, awayStore{id: e.Store, failure: e.Failure})
		}
	case StoreRead:
		out.markRead(e.Store)
	case StoreForgotten:
		reqs = out.forgotten(e)
	case Closed:
		out.remove(e.Ref)
	case Requeued:
		reqs = out.requeued(e.Ref)
	}
	reqs = append(reqs, out.schedule()...)
	return out, reqs
}

// forgotten gives up a store's jobs. A job whose command may still run stays, in PhaseLeft, until a
// look finds that command ended or its limit passes.
func (s *State) forgotten(e StoreForgotten) []Request {
	s.markRead(e.Store)
	was := map[Ref]entry{}
	for _, cur := range s.entries {
		if cur.ref.Store == e.Store {
			was[cur.ref] = cur
		}
	}
	s.entries = slices.DeleteFunc(s.entries, func(cur entry) bool { return cur.ref.Store == e.Store })
	var reqs []Request
	for _, run := range e.Left {
		if run.Ref.Store != e.Store || s.find(run.Ref) >= 0 {
			continue
		}
		left, known := was[run.Ref]
		if !known {
			s.seq++
			left.seq = s.seq
		}
		left = entry{ref: run.Ref, submittedAt: left.submittedAt, seq: left.seq, phase: PhaseLeft, holdUntil: left.holdUntil, work: left.work}
		left.judgeHold(run.Supervisor, e.At)
		if !left.holds {
			continue
		}
		s.entries = append(s.entries, left)
		reqs = append(reqs, s.awaitRetry(len(s.entries)-1)...)
	}
	return reqs
}

// recoverLeft judges again whether a left job's command can still run.
func (s *State) recoverLeft(i int, e Recovered) []Request {
	cur := &s.entries[i]
	cur.judgeHold(e.Supervisor, e.At)
	if !cur.holds {
		s.remove(e.Ref)
		return nil
	}
	return s.awaitRetry(i)
}

func (s *State) find(ref Ref) int {
	return slices.IndexFunc(s.entries, func(e entry) bool { return e.ref == ref })
}

func (s *State) remove(ref Ref) {
	if i := s.find(ref); i >= 0 {
		s.entries = slices.Delete(s.entries, i, i+1)
	}
}

func (s *State) admit(e Admitted) {
	if s.find(e.Ref) >= 0 {
		return
	}
	s.seq++
	s.entries = append(s.entries, entry{ref: e.Ref, submittedAt: e.SubmittedAt, seq: s.seq, phase: PhaseQueued, attempts: e.Attempts, policy: e.Policy, work: e.Work})
}

func (s *State) isUnread(id StoreID) bool {
	return slices.ContainsFunc(s.unread, func(a awayStore) bool { return a.id == id })
}

func (s *State) markRead(id StoreID) {
	s.unread = slices.DeleteFunc(s.unread, func(a awayStore) bool { return a.id == id })
}

// recover applies what a look at a job and its run found. Only a job in doubt is looked at twice,
// and a queued one can have a run left: any other job the machine holds is already on its way.
func (s *State) recover(e Recovered) []Request {
	var kept entry
	if i := s.find(e.Ref); i >= 0 {
		switch s.entries[i].phase {
		case PhaseQueued:
			return s.recoverQueued(e)
		case PhaseInDoubt:
			kept = s.entries[i]
			s.remove(e.Ref)
		case PhaseLeft:
			return s.recoverLeft(i, e)
		case PhaseDiscarding, PhaseLaunching, PhaseRunning, PhaseStopping, PhaseSettling, PhaseCancelling, PhaseUnrecorded:
			return nil
		}
	}
	switch f := e.Finding.(type) {
	case JobIn:
		switch f.State {
		case core.JobQueued:
			return s.recoverQueuedAfresh(e, kept)
		case core.JobRunning:
			return s.recoverRunning(e, kept)
		default:
			return s.recoverEnded(e)
		}
	case NoSuchJob:
		return s.recoverEnded(e)
	default:
		// The store cannot say. A job the machine knows stays in it, and so does a run left over,
		// which is only known to be nobody's once the store says so. With no run and no job there
		// is nothing to keep: the store's own mark holds back its jobs.
		if _, none := e.Supervisor.(RunNone); none && kept.seq == 0 {
			return nil
		}
		return s.inDoubt(e, kept)
	}
}

// recoverRunning places a job its store has running, by what is left of its supervisor.
func (s *State) recoverRunning(e Recovered, kept entry) []Request {
	switch p := e.Supervisor.(type) {
	case RunAlive:
		if p.Phase == RunParked {
			// Its command has not started, so it starts in its turn: another job may run by now.
			s.place(e, kept, PhaseInDoubt)
			cur := &s.entries[s.find(e.Ref)]
			cur.parked, cur.trouble = true, core.Failure{Kind: core.FailureUnavailable, Message: "its command waits for the machine"}
			return nil
		}
		s.place(e, kept, PhaseRunning)
		cur := &s.entries[s.find(e.Ref)]
		cur.cancelSent = cur.cancelAsked
		return []Request{Adopt{Ref: e.Ref, CancelFirst: cur.cancelAsked, CancelReason: cur.cancelReason, Leases: s.leasesOf(*cur)}}
	case RunDead:
		if !p.Proof.Valid() {
			return s.doubtUnlessGivenUp(e, kept)
		}
		// Its supervisor is dead, so the command ended with it, and the attempt was cut off.
		s.place(e, kept, PhaseRunning)
		return s.cutOff(s.find(e.Ref), p.Proof, p.Start, e.At)
	default:
		return s.doubtUnlessGivenUp(e, kept)
	}
}

// doubtUnlessGivenUp keeps a running job the machine cannot judge in doubt, unless its store gave
// up the place that ran it. Such a job is lost as soon as nothing found of it here can be running
// its command: at once with no run here, and once its limit passed with one this build cannot use.
// The user's word is the proof for the other place, and the limit is the proof for this one. A
// limit found by an earlier look stands, as a run seen here may be out of sight now.
func (s *State) doubtUnlessGivenUp(e Recovered, kept entry) []Request {
	if f, ok := e.Finding.(JobIn); ok && f.GivenUp {
		probe := kept
		probe.judgeHold(e.Supervisor, e.At)
		if !probe.holds && (probe.holdUntil.IsZero() || !e.At.Before(probe.holdUntil)) {
			s.place(e, kept, PhaseRunning)
			return s.settle(e.Ref, givenUpJob(e.Ref.Job, e.At))
		}
	}
	return s.inDoubt(e, kept)
}

// recoverEnded handles a job that needs the daemon no more: a run left over holds nothing of value,
// once it is known to be what the store says. One the daemon cannot judge is left alone.
func (s *State) recoverEnded(e Recovered) []Request {
	switch e.Supervisor.(type) {
	case RunAlive, RunDead:
		return []Request{Discard{Ref: e.Ref}}
	default:
		return nil
	}
}

// recoverQueued handles a look at a job that is queued in the machine, which can have a run left.
func (s *State) recoverQueued(e Recovered) []Request {
	if f, ok := e.Finding.(JobIn); !ok || f.State != core.JobQueued {
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
	switch e.Supervisor.(type) {
	case RunNone:
		s.place(e, kept, PhaseQueued)
		if s.entries[s.find(e.Ref)].cancelAsked {
			return s.cancelQueuedJob(e.Ref, e.At)
		}
		return nil
	case RunAlive, RunDead:
		s.place(e, kept, PhaseDiscarding)
		return []Request{Discard{Ref: e.Ref, Queued: true}}
	default:
		return s.inDoubt(e, kept)
	}
}

// place puts the job in the machine in a phase, keeping what it was told before.
func (s *State) place(e Recovered, kept entry, phase Phase) {
	if kept.seq == 0 {
		s.seq++
		kept = entry{ref: e.Ref, seq: s.seq}
		if f, ok := e.Finding.(JobIn); ok {
			kept.submittedAt = f.SubmittedAt
		}
	}
	if f, ok := e.Finding.(JobIn); ok {
		// The store keeps the attempts, so its word on them is the latest.
		kept.attempts, kept.policy = f.Attempts, f.Policy
		if f.Work.told() {
			kept.work = f.Work
		}
	}
	kept.phase, kept.waiting, kept.holds, kept.parked, kept.placed = phase, false, false, false, nil
	// No retry is owed to a job placed afresh, so one refused a lease tries again in its turn.
	kept.leased = nil
	if phase != PhaseInDoubt {
		kept.holdUntil = time.Time{}
	}
	s.entries = append(s.entries, kept)
}

// inDoubt keeps a job the machine cannot judge, and asks for another look.
func (s *State) inDoubt(e Recovered, kept entry) []Request {
	s.place(e, kept, PhaseInDoubt)
	i := s.find(e.Ref)
	cur := &s.entries[i]
	cur.trouble = core.Failure{Kind: core.FailureUnavailable, Message: doubtOf(e)}
	cur.judgeHold(e.Supervisor, e.At)
	reqs := s.awaitRetry(i)
	// A cancel needs the supervisor only, so it goes on while the store cannot be read.
	if _, alive := e.Supervisor.(RunAlive); alive && cur.cancelAsked && !cur.cancelSent {
		cur.cancelSent = true
		reqs = append(reqs, SignalCancel{Ref: e.Ref, Reason: cur.cancelReason})
	}
	return reqs
}

// doubtOf says why a look left a job in doubt.
func doubtOf(e Recovered) string {
	if _, away := e.Finding.(StoreAway); away {
		return "its store cannot be read"
	}
	if _, none := e.Supervisor.(RunNone); none {
		return "the store has it running and there is no sign of a run here: if the place that ran it is gone for good, remove the store's owner record and the job is marked lost"
	}
	return "its supervisor gives no answer this build can use, and may still run: if the place that ran it is gone for good, remove the store's owner record and the job is marked lost once its time limit has passed"
}

// judgeHold decides whether a job in doubt holds the machine, from what was found of its
// supervisor at a moment. A command that cannot be using the machine holds nothing. One that can
// holds it for at most its limit from the first look, as the command ends by then.
func (e *entry) judgeHold(p Presence, at time.Time) {
	var limit time.Duration
	switch p := p.(type) {
	case RunAlive:
		switch p.Phase {
		case RunRunning:
			limit = p.Limit
		case RunParked, RunFinished, RunReleased:
		}
	case RunUnknown:
		limit = p.Limit
	case RunNone, RunDead:
	}
	if limit <= 0 {
		e.holds = false
		return
	}
	if e.holdUntil.IsZero() {
		e.holdUntil = at.Add(limit)
	}
	e.holds = at.Before(e.holdUntil)
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
		s.admit(Admitted{Ref: e.Ref, SubmittedAt: e.SubmittedAt})
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
	case PhaseSettling, PhaseUnrecorded:
		// Its end is decided already, unless it is queued again: then the cancel is its end, once
		// the store has it queued and its old run is released.
		if cur.settlement.Requeues() && !cur.cancelAsked {
			cur.cancelAsked, cur.cancelReason = true, e.Reason
		}
	case PhaseStopping, PhaseCancelling:
		// Its end is decided already.
	case PhaseLeft:
		// Its store is given up, and the shell asked its supervisor to end it already.
	}
	return nil
}

// cancelFailed offers a cancel that did not arrive again, after a while.
func (s *State) cancelFailed(ref Ref) []Request {
	i := s.find(ref)
	if i < 0 || (s.entries[i].phase != PhaseRunning && s.entries[i].phase != PhaseInDoubt) {
		return nil
	}
	s.entries[i].cancelSent = false
	return s.awaitRetry(i)
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
	s.entries[i].phase, s.entries[i].placed = PhaseQueued, nil
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
	return []Request{Adopt{Ref: ref, CancelFirst: cur.cancelAsked, CancelReason: cur.cancelReason, Leases: s.leasesOf(*cur)}}
}

func (s *State) launchFailed(e LaunchFailed) []Request {
	i := s.find(e.Ref)
	if i < 0 || s.entries[i].phase != PhaseLaunching {
		return nil
	}
	cur := &s.entries[i]
	if cur.cancelAsked {
		// The job never ran, so the cancel the user asked for is the end it gets.
		st := cancelQueued(e.Ref.Job, cur.cancelReason, e.At)
		if e.Cause == CauseStoreAway {
			// Its store may have it running. The shell moves it from what the store shows, and
			// until then the rest of the store waits, as for any job shown running.
			st.Transition.From = core.JobRunning
		}
		return s.settle(e.Ref, st)
	}
	switch e.Cause {
	case CauseStoreAway:
		// The store may or may not have it running, and any supervisor is parked, so nothing runs
		// and nothing is lost. A look says which, and the job goes on from there.
		cur.phase, cur.trouble = PhaseInDoubt, e.Failure
		return s.awaitRetry(i)
	case CauseSupervisorSilent:
		// The store still has it queued, so nothing of it ran: it is tried again, after whatever
		// the launch left is released, a bounded number of times.
		cur.spawnFailures++
		if cur.spawnFailures < core.MaxAttempts {
			cur.phase, cur.trouble = PhaseDiscarding, e.Failure
			return []Request{Discard{Ref: e.Ref, Queued: true}}
		}
	case CauseRefused, CauseUnprepared:
	}
	return s.settle(e.Ref, failedToLaunch(e.Ref.Job, e.Failure, e.At))
}

// leaseRefused puts a job that could not take a lease back in the queue, where it waits, keeping
// its turn, for a RetryDue to try again. Nothing of it ran, so a cancel that came in the meantime is
// its end.
func (s *State) leaseRefused(e LeaseRefused) []Request {
	i := s.find(e.Ref)
	if i < 0 || s.entries[i].phase != PhaseLaunching {
		return nil
	}
	cur := &s.entries[i]
	if cur.cancelAsked {
		return s.settle(e.Ref, cancelQueued(e.Ref.Job, cur.cancelReason, e.At))
	}
	cur.phase, cur.placed = PhaseQueued, nil
	cur.leased = &schedule.LeasedOut{Resource: e.Resource, Holder: e.Holder, Detail: e.Detail}
	return s.awaitRetry(i)
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
	default:
		return nil
	}
}

func (s *State) lost(e SupervisorLost) []Request {
	i := s.find(e.Ref)
	if i < 0 || (s.entries[i].phase != PhaseRunning && s.entries[i].phase != PhaseStopping) {
		return nil
	}
	if !e.Proof.Valid() {
		return s.lookAgain(i, core.Failure{Kind: core.FailureUnavailable, Message: "its supervisor stopped answering, which proves nothing"})
	}
	if s.entries[i].phase == PhaseStopping {
		// The daemon was ending the job, so it is not run again.
		return s.settle(e.Ref, lostJob(e.Ref.Job, e.At))
	}
	return s.cutOff(i, e.Proof, e.Start, e.At)
}

// cutOff settles the running job at i, whose supervisor is proved gone, as CutOff says. A job the user
// asked to cancel is never run again.
func (s *State) cutOff(i int, proof DeathProof, start Start, at time.Time) []Request {
	cur := s.entries[i]
	if cur.cancelAsked {
		return s.settle(cur.ref, lostJob(cur.ref.Job, at))
	}
	st := CutOff(cur.ref.Job, cur.policy, cur.attempts, proof, start, at)
	if !st.Requeues() {
		return s.settle(cur.ref, st)
	}
	// The dead run is released once the store has the job queued, before the job is launched again.
	s.entries[i].phase, s.entries[i].settlement, s.entries[i].release = PhaseSettling, st, false
	return []Request{Move{Ref: cur.ref, Settlement: st}}
}

// requeued takes a job whose cut-off attempt is recorded back to the queue, in its old place, once
// the run that was cut off is released.
func (s *State) requeued(ref Ref) []Request {
	i := s.find(ref)
	if i < 0 || !s.entries[i].settlement.Requeues() || (s.entries[i].phase != PhaseSettling && s.entries[i].phase != PhaseUnrecorded) {
		return nil
	}
	cur := &s.entries[i]
	cur.attempts++
	cur.phase, cur.settlement, cur.cancelSent, cur.holds, cur.holdUntil = PhaseDiscarding, Settlement{}, false, false, time.Time{}
	cur.trouble = core.Failure{Kind: core.FailureUnavailable, Message: "the run of its attempt that was cut off is being released"}
	return []Request{Discard{Ref: ref, Queued: true}}
}

// lookAgain takes the job at i out of the daemon's hands until a look says more. Until then it
// holds the machine, as its command may run.
func (s *State) lookAgain(i int, trouble core.Failure) []Request {
	cur := &s.entries[i]
	cur.phase, cur.trouble, cur.holds = PhaseInDoubt, trouble, true
	return []Request{Reexamine{Ref: cur.ref}}
}

func (s *State) followFailed(e FollowFailed) []Request {
	i := s.find(e.Ref)
	if i < 0 || s.entries[i].phase != PhaseRunning {
		return nil
	}
	switch e.Cause {
	case CauseStoreAway:
		// Nothing is wrong with the job. A fenced owner comes here too, and cancels nothing that a
		// new owner may have adopted.
		return s.lookAgain(i, e.Failure)
	case CauseSupervisorSilent, CauseRefused, CauseUnprepared:
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
	s.entries[i].phase, s.entries[i].trouble, s.entries[i].holds = PhaseInDoubt, e.Failure, true
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
	default:
		return nil
	}
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
	case PhaseInDoubt, PhaseLeft:
		return []Request{Reexamine{Ref: ref}}
	case PhaseDiscarding:
		return []Request{Discard{Ref: ref, Queued: true}}
	case PhaseRunning:
		if cur.cancelAsked && !cur.cancelSent {
			cur.cancelSent = true
			return []Request{SignalCancel{Ref: ref, Reason: cur.cancelReason}}
		}
	case PhaseQueued:
		// A job refused a lease tries again: the scheduler considers it at the end of this step.
		cur.leased = nil
	case PhaseLaunching, PhaseStopping, PhaseSettling, PhaseCancelling:
		// What it would repeat is in flight.
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

// adoptCancelled takes up the jobs whose command waits in a parked supervisor and whose cancel was
// asked for. A cancel does not wait for the job's turn: it needs the supervisor only, and no
// command starts.
func (s *State) adoptCancelled() []Request {
	var reqs []Request
	for i := range s.entries {
		if cur := &s.entries[i]; cur.parked && cur.cancelAsked {
			reqs = append(reqs, s.adopt(i))
		}
	}
	return reqs
}

// adopt takes up the parked job at i.
func (s *State) adopt(i int) Request {
	cur := &s.entries[i]
	cur.phase, cur.parked, cur.cancelSent, cur.holdUntil = PhaseRunning, false, cur.cancelAsked, time.Time{}
	return Adopt{Ref: cur.ref, CancelFirst: cur.cancelAsked, CancelReason: cur.cancelReason, Leases: s.leasesOf(*cur)}
}

// schedule starts what the scheduler admits: the jobs with a parked supervisor first, then the
// queued jobs whose store does not hold them back. It notes why each of the others waits.
func (s *State) schedule() []Request {
	reqs := s.adoptCancelled()
	for i := range s.entries {
		s.entries[i].wait, s.entries[i].waits, s.entries[i].refused = schedule.WaitReason{}, false, ""
	}
	if s.recovering {
		return reqs
	}
	st := schedule.State{Resources: s.resources(), Recent: s.recent}
	for _, e := range s.entries {
		switch {
		case e.holdsMachine():
			st.Holdings = append(st.Holdings, schedule.Holding{ID: idOf(e.ref), Claims: s.heldClaims(e), Run: e.work.Run})
		case e.parked:
			st.Waiting = append(st.Waiting, s.candidate(e, true))
		case e.phase == PhaseQueued && !s.storeBlocked(e.ref.Store):
			st.Waiting = append(st.Waiting, s.candidate(e, false))
		}
	}
	rules := schedule.Rules{Policy: s.scheduling.Policy, Placer: s.scheduling.Placer, Filters: s.scheduling.Filters}
	for _, d := range schedule.Step(st, rules) {
		i := slices.IndexFunc(s.entries, func(e entry) bool { return idOf(e.ref) == d.Target() })
		cur := &s.entries[i]
		switch d := d.(type) {
		case schedule.Start:
			placed := d.Option
			cur.placed = &placed
			s.recent = schedule.Remember(s.recent, schedule.Grant{Run: cur.work.Run, Claims: placed.Claims})
			if cur.parked {
				reqs = append(reqs, s.adopt(i))
				continue
			}
			cur.phase = PhaseLaunching
			reqs = append(reqs, LaunchRequest{Ref: cur.ref, Placement: placed.Placement, Leases: s.leasesOf(*cur)})
		case schedule.Wait:
			cur.wait, cur.waits = d.Reason, true
		case schedule.Refuse:
			cur.refused = d.Detail
		}
	}
	return reqs
}

// resources are what jobs claim. A state made without a Scheduling, the zero State included, runs
// one job at a time.
func (s *State) resources() []schedule.Resource {
	if len(s.scheduling.Resources) == 0 {
		return OneAtATime().Resources
	}
	return s.scheduling.Resources
}

func idOf(ref Ref) schedule.ID { return schedule.ID(ref.String()) }

func (s *State) candidate(e entry, first bool) schedule.Candidate {
	return schedule.Candidate{ID: idOf(e.ref), Options: s.optionsOf(e), Run: e.work.Run, Submitted: e.submittedAt, Seq: e.seq, First: first, Leased: e.leased}
}

// leasesOf is the resources a job holds alone, each once and in order: those whose leases it takes.
func (s *State) leasesOf(e entry) []schedule.ResourceID {
	var out []schedule.ResourceID
	for _, c := range s.heldClaims(e) {
		if c.Exclusive && !slices.Contains(out, c.Resource) {
			out = append(out, c.Resource)
		}
	}
	slices.Sort(out)
	return out
}

// optionsOf is where a job may run: its work's options, or, for a job whose work the machine was
// not told, anywhere, holding every resource for itself alone.
func (s *State) optionsOf(e entry) []schedule.Option {
	if e.work.told() {
		return e.work.Options
	}
	out := make([]schedule.Claim, 0, len(s.scheduling.Resources))
	for _, r := range s.resources() {
		out = append(out, schedule.Claim{Resource: r.ID, Amount: 1, Exclusive: true})
	}
	return schedule.Only(out...)
}

// heldClaims is what a job holds while it holds the machine: the claims of the option the scheduler
// started it on, or, for a job the machine holds without having started it (one that runs, or
// might, as a restart found it), the claims of every place it may run, which is the one it started
// on for a job whose store says where.
func (s *State) heldClaims(e entry) []schedule.Claim {
	if e.placed != nil {
		return e.placed.Claims
	}
	options := s.optionsOf(e)
	if len(options) == 1 {
		return options[0].Claims
	}
	// Of a resource two places claim, it holds the most either claims.
	var out []schedule.Claim
	for _, o := range options {
		for _, c := range o.Claims {
			i := slices.IndexFunc(out, func(h schedule.Claim) bool { return h.Resource == c.Resource })
			if i < 0 {
				out = append(out, c)
				continue
			}
			out[i].Amount, out[i].Exclusive = max(out[i].Amount, c.Amount), out[i].Exclusive || c.Exclusive
		}
	}
	return out
}

// holdsMachine reports whether a job holds its claims: its command may run, or its end is on its
// way to the store.
func (e entry) holdsMachine() bool {
	switch e.phase {
	case PhaseQueued, PhaseDiscarding, PhaseCancelling, PhaseUnrecorded:
		return false
	case PhaseInDoubt, PhaseLeft:
		return e.holds
	case PhaseLaunching, PhaseRunning, PhaseStopping, PhaseSettling:
		return true
	}
	return true
}

// storeBlocked reports whether a job of the store may not start: the store was not read, or it can
// show a job running that has ended or that the daemon cannot judge.
func (s *State) storeBlocked(id StoreID) bool {
	return s.isUnread(id) || slices.ContainsFunc(s.entries, func(e entry) bool {
		return e.ref.Store == id && e.blocksStore()
	})
}

// A parked job in doubt does not block its store: it is known, its supervisor is parked, and it is
// the first job the machine takes up when it is free.
func (e entry) blocksStore() bool {
	return (e.phase == PhaseInDoubt && !e.parked) || (e.phase == PhaseUnrecorded && e.settlement.Transition.From == core.JobRunning)
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

// Holds says whether the machine holds a job's claims: from its launch until its end is recorded,
// and for a job in doubt while its command can run. The shell keeps the job's leases exactly as
// long.
func (s State) Holds(ref Ref) bool {
	i := s.find(ref)
	return i >= 0 && s.entries[i].holdsMachine()
}

// Parked says the job waits for its turn with a parked supervisor, which it will need.
func (s State) Parked(ref Ref) bool {
	i := s.find(ref)
	return i >= 0 && s.entries[i].parked
}

// Trouble is a job that cannot go on until something outside the daemon does: why, and which job.
type Trouble struct {
	Ref     Ref
	Failure core.Failure
}

// Troubled returns the jobs in PhaseUnrecorded and PhaseInDoubt. Whoever waits for one of them has
// no end to wait for until the trouble passes, and is told so. A parked job is not here: nothing is
// wrong with it, it starts in its turn like a queued job, and Waiting says what holds its turn up
// when that is a trouble.
func (s State) Troubled() []Trouble {
	var out []Trouble
	for _, e := range s.entries {
		if e.phase == PhaseUnrecorded || (e.phase == PhaseInDoubt && !e.parked) {
			out = append(out, Trouble{Ref: e.ref, Failure: e.trouble})
		}
	}
	return out
}

// Counts returns how many jobs wait for the machine and how many are past waiting. A job counts as
// running from the moment the machine takes it, before its store says so, so that no reader of the
// store sees a running job that the counts have as queued. It goes on counting until its end is
// recorded. A discarding job waits: its store still has it queued.
func (s State) Counts() (queued, running int) {
	for _, e := range s.entries {
		switch e.phase {
		case PhaseQueued, PhaseDiscarding, PhaseCancelling:
			queued++
		default:
			running++
		}
	}
	return queued, running
}

// Unread returns the stores whose jobs the machine has not read, for the sweep to read.
func (s State) Unread() []StoreID {
	out := make([]StoreID, 0, len(s.unread))
	for _, a := range s.unread {
		out = append(out, a.id)
	}
	return out
}

// Waiting returns the queued and parked jobs that wait on another job's or their own store's
// trouble, and why. Each will start when that passes, and its waiter is told. A job that only
// waits its turn behind a job that runs is not here. Neither is a job that waits for one that is
// here, when nothing else holds it back: what it waits for is in the daemon's hands. A job whose
// claims can never be granted is here, as nothing the daemon does will start it.
func (s State) Waiting() []Trouble {
	var out []Trouble
	troubles := map[schedule.ID]string{}
	for _, e := range s.entries {
		if e.phase != PhaseQueued && !e.parked {
			continue
		}
		if why, ok := s.waitsOn(e, troubles); ok {
			troubles[idOf(e.ref)] = why
			out = append(out, Trouble{Ref: e.ref, Failure: core.Failure{Kind: core.FailureUnavailable, Message: why}})
		}
	}
	return out
}

// waitsOn says what trouble a waiting job waits for. troubles are those of the jobs before it.
func (s State) waitsOn(e entry, troubles map[schedule.ID]string) (string, bool) {
	if i := slices.IndexFunc(s.unread, func(a awayStore) bool { return a.id == e.ref.Store }); i >= 0 {
		return fmt.Sprintf("it stays queued until its store can be read: %s", s.unread[i].failure.Message), true
	}
	i := slices.IndexFunc(s.entries, func(o entry) bool { return o.ref.Store == e.ref.Store && o.blocksStore() })
	if i >= 0 {
		o := s.entries[i]
		return fmt.Sprintf("it stays queued until job %s of its store is settled: %s", o.ref.Job, o.trouble.Message), true
	}
	if e.refused != "" {
		return fmt.Sprintf("it can never start: %s", e.refused), true
	}
	if !e.waits {
		return "", false
	}
	switch e.wait.Kind {
	case core.WaitBehind:
		// It waits for the job before it, whose trouble, if it has one, is its own.
		for _, b := range e.wait.Blockers {
			if why, ok := troubles[b]; ok {
				return why, true
			}
		}
		return "", false
	case core.WaitFull, core.WaitHeldExclusively, core.WaitInUse:
	default:
		return "", false
	}
	// It waits for the jobs that hold what it needs. Only when each of them is in doubt is that
	// trouble: anything else that holds it ends in the daemon's hands.
	var first *entry
	for _, b := range e.wait.Blockers {
		j := slices.IndexFunc(s.entries, func(o entry) bool { return idOf(o.ref) == b })
		if j < 0 || s.entries[j].phase != PhaseInDoubt {
			return "", false
		}
		if first == nil {
			first = &s.entries[j]
		}
	}
	if first == nil {
		return "", false
	}
	o := *first
	if o.holdUntil.IsZero() {
		return fmt.Sprintf("it stays queued while the daemon finds out about job %s: %s", o.ref, o.trouble.Message), true
	}
	return fmt.Sprintf("it stays queued until job %s ends, or until %s at the latest: %s", o.ref, o.holdUntil.UTC().Format(time.RFC3339), o.trouble.Message), true
}

// Blocker is a job that stands in another's way: Work says what it is.
type Blocker struct {
	Ref  Ref
	Work Work
}

// Queued is why a queued or parked job did not start at the last step, as the scheduler said.
type Queued struct {
	Ref    Ref
	Reason schedule.WaitReason
	// Blockers are the jobs of Reason.Blockers that the machine holds.
	Blockers []Blocker
}

// Queue returns why each queued or parked job that the scheduler held back waits, in the order the
// machine heard of them. A job held back by trouble or by its store is not here: Waiting says why.
func (s State) Queue() []Queued {
	var out []Queued
	for _, e := range s.entries {
		if !e.waits {
			continue
		}
		q := Queued{Ref: e.ref, Reason: e.wait}
		for _, b := range e.wait.Blockers {
			if j := slices.IndexFunc(s.entries, func(o entry) bool { return idOf(o.ref) == b }); j >= 0 {
				q.Blockers = append(q.Blockers, Blocker{Ref: s.entries[j].ref, Work: s.entries[j].work})
			}
		}
		out = append(out, q)
	}
	return out
}

// Stranded returns the queued jobs that nothing will ever start, as of a moment. It is empty unless
// a job waits for a free machine that nobody owes it: nothing holds the machine, or all that does
// is a job in doubt past its bound, and the job's store does not hold it back. Every other job the
// machine holds waits for an answer the shell owes it (a launch, a discard, a supervisor's outcome,
// a look, a retry), so a state with no stranded job has no orphan wait. A job that another holder's
// lease keeps waiting is owed a retry.
func (s State) Stranded(at time.Time) []Ref {
	owed := func(e entry) bool {
		bounded := e.phase == PhaseInDoubt || e.phase == PhaseLeft
		// A job refused a lease is owed a retry, which schedules it and those behind it again.
		leaseRetry := e.phase == PhaseQueued && e.leased != nil && e.armed && e.waiting
		return leaseRetry || (e.holdsMachine() && (!bounded || e.holdUntil.IsZero() || at.Before(e.holdUntil)))
	}
	if s.recovering || slices.ContainsFunc(s.entries, owed) {
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
