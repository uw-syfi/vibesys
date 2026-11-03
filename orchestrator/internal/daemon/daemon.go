package daemon

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"math"
	"time"

	"google.golang.org/protobuf/proto"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/jobs"
	"github.com/uw-syfi/vibesys/orchestrator/internal/store"
	"github.com/uw-syfi/vibesys/orchestrator/internal/wire"
)

// Config is what a Daemon is made of.
type Config struct {
	Engine   EngineDeps
	Sidecars Sidecars
	// Jobs is the job source. Nil means there is none: no job is ever reported, which is the truth
	// for a build with no vibesys-cli wired in, and not the same as a source that reports no jobs.
	Jobs      jobs.JobSource
	Authority Authority
	// Bounds limits every call the daemon makes outside itself. It is required.
	Bounds Bounds
	// AgentCLI is the absolute path of the vibesys-cli every agent is told to run, or empty when
	// there is none to name.
	AgentCLI string
	// AgentSocket is the path of the agent-only socket, told to every session the daemon launches. The
	// operator's socket is not known here, so no environment can carry it.
	AgentSocket string
	// JobPollEvery is how often the job source is asked, measured on the Engine's clock. Zero polls
	// on every step.
	JobPollEvery time.Duration
	// Batch bounds the effects claimed and the sidecar events read in one step. Zero means 64.
	Batch int
}

// Daemon is the Engine and everything that turns its effects into I/O and the world's events into
// inputs. Its methods are for one goroutine, the loop; the Engine is safe to share.
type Daemon struct {
	*Engine
	cfg     Config
	conns   map[core.Session]Sidecar
	follows map[core.Session]*follow
	polled  time.Time
	// toAttach holds the sessions of a previous daemon whose sidecar this one has not found yet. A
	// live session in neither conns nor toAttach is waiting for its launch, which is in the outbox.
	toAttach map[core.Session]bool
	// deltaThrough is the highest sidecar offset whose delta was shown, per session, so that a
	// stream reopened after the committed offset does not show its deltas twice.
	deltaThrough map[core.Session]uint64
	// done and retry are effects that were carried out (or not) and whose outbox rows could not be
	// settled yet. They are settled first on the next step.
	done, retry []store.OutboxID
}

// follow is one open Follow, and the event that could not be committed yet.
type follow struct {
	stream EventStream
	stash  *eventStash
}

type eventStash struct {
	data   []byte
	offset uint64
}

// Open recovers the state and returns a daemon. Call Reattach before the first Step.
func Open(cfg Config) (*Daemon, error) {
	if cfg.Batch <= 0 {
		cfg.Batch = 64
	}
	if cfg.Bounds == nil {
		return nil, errors.New("daemon: Config.Bounds is required: a call to the world without a bound can stall the whole loop")
	}
	engine, err := OpenEngine(cfg.Engine)
	if err != nil {
		return nil, err
	}
	return &Daemon{
		Engine: engine, cfg: cfg,
		conns: map[core.Session]Sidecar{}, follows: map[core.Session]*follow{},
		toAttach: map[core.Session]bool{}, deltaThrough: map[core.Session]uint64{},
	}, nil
}

// liveSessions are the sessions that have a say: the current one of each agent that is not over.
func liveSessions(s core.State) []core.Session {
	var out []core.Session
	for _, run := range s.Runs() {
		for _, a := range s.Agents(run.ID) {
			if a.Session > 0 && !a.Status.Over() {
				out = append(out, core.Session{Agent: a.ID, N: a.Session})
			}
		}
	}
	return out
}

// overSessions are the current sessions of the agents that are over, which have no more say.
func overSessions(s core.State) []core.Session {
	var out []core.Session
	for _, run := range s.Runs() {
		for _, a := range s.Agents(run.ID) {
			if a.Session > 0 && a.Status.Over() {
				out = append(out, core.Session{Agent: a.ID, N: a.Session})
			}
		}
	}
	return out
}

func isLive(s core.State, session core.Session) bool {
	a, ok := s.Agent(session.Agent)
	return ok && a.Session == session.N && !a.Status.Over()
}

// Reattach is what a restart does besides recovering the state. It first carries out every effect
// the last daemon left in the outbox, so that a session whose launch was committed and not yet
// carried out is launched and not mistaken for a session whose sidecar died. It then looks for the
// sidecars of the other live sessions. One whose sidecar is gone is lost, which the core answers by
// resuming it or giving up. A call that fails or runs out of time leaves its session to the next
// step, so the error is a report and the daemon may serve regardless. It is safe to repeat.
func (d *Daemon) Reattach(ctx context.Context) error {
	var errs []error
	claimed, err := d.Store.Claim(d.Clock.Now(), math.MaxInt32)
	if err != nil {
		return fmt.Errorf("claim the effects of the last daemon: %w", err)
	}
	// Every live session is to be found, except the ones whose launch is in the outbox: the delivery
	// below launches those, and a launch that has to be retried keeps its session out of the search.
	for _, session := range liveSessions(d.State()) {
		d.toAttach[session] = true
	}
	launching, err := d.deliver(ctx, claimed)
	errs = append(errs, err)
	for session := range launching {
		delete(d.toAttach, session)
	}
	for _, session := range liveSessions(d.State()) {
		if d.toAttach[session] {
			_, err := d.sidecar(ctx, session)
			errs = append(errs, err)
		}
	}
	// A daemon that stopped between committing the end of a session and ending its sidecar left that
	// sidecar running.
	for _, session := range overSessions(d.State()) {
		errs = append(errs, d.releaseUnattached(ctx, session))
	}
	return errors.Join(errs...)
}

// errLaunchPending says a session has no sidecar yet because its launch has not been carried out.
var errLaunchPending = errors.New("the session's launch has not been carried out yet")

// sidecar returns the connection to a live session's sidecar, attaching it if this daemon has not
// met it. A session whose sidecar is gone is reported lost and returns a nil Sidecar and no error.
// A session whose launch is still to be carried out returns errLaunchPending: it is not lost.
func (d *Daemon) sidecar(ctx context.Context, session core.Session) (Sidecar, error) {
	if sc, ok := d.conns[session]; ok {
		return sc, nil
	}
	if !d.toAttach[session] {
		return nil, fmt.Errorf("%s/%d: %w", session.Agent, session.N, errLaunchPending)
	}
	sc, err := bound(d.cfg.Bounds, ctx, func(c context.Context) (Sidecar, error) {
		return d.cfg.Sidecars.Attach(c, session)
	})
	switch {
	case errors.Is(err, ErrSidecarGone):
		return nil, d.lose(session, "the session's sidecar was not found when the daemon looked for it")
	case err != nil:
		return nil, fmt.Errorf("attach %s/%d: %w", session.Agent, session.N, err)
	}
	d.conns[session] = sc
	delete(d.toAttach, session)
	return sc, nil
}

// maxDetail bounds the reason kept with a lost session.
const maxDetail = 500

// lose reports a session's sidecar gone and forgets what the daemon held for it. detail says why.
func (d *Daemon) lose(session core.Session, detail string) error {
	d.forget(session)
	if r := []rune(detail); len(r) > maxDetail {
		detail = string(r[:maxDetail]) + "..."
	}
	_, err := d.Apply(core.SidecarLost{Session: session, Detail: detail})
	return err
}

// release ends the sidecar process of a session that is no longer live, and forgets what the daemon
// held for it. Whatever made the session over (a close the sidecar reported, a failure, a stop that
// went unanswered, a resume that replaced it), the process is the daemon's to end: the core has
// accounted for the session, and a sidecar left running would hold its agent's process groups and
// socket until its own linger ran out. It is safe to repeat, and a call that fails or outlives its
// bound leaves the session held, so the next step tries again.
func (d *Daemon) release(ctx context.Context, session core.Session) error {
	sc, ok := d.conns[session]
	if !ok {
		d.forget(session)
		return nil
	}
	if err := bounded(d.cfg.Bounds, ctx, sc.Kill); err != nil && !errors.Is(err, ErrSidecarGone) {
		return fmt.Errorf("release %s/%d: %w", session.Agent, session.N, err)
	}
	d.forget(session)
	return nil
}

// releaseUnattached ends the sidecar of a session this daemon holds no connection to, which a
// previous daemon may have left running: an over session found at a restart, or the session a
// resume replaced.
func (d *Daemon) releaseUnattached(ctx context.Context, session core.Session) error {
	if _, held := d.conns[session]; held {
		return nil // the next step's sweep releases it.
	}
	sc, err := bound(d.cfg.Bounds, ctx, func(c context.Context) (Sidecar, error) {
		return d.cfg.Sidecars.Attach(c, session)
	})
	switch {
	case errors.Is(err, ErrSidecarGone):
		return nil
	case err != nil:
		return fmt.Errorf("find the sidecar of %s/%d to release it: %w", session.Agent, session.N, err)
	}
	d.conns[session] = sc // not live, so the next step's sweep ends it, and retries if that fails.
	return nil
}

func (d *Daemon) forget(session core.Session) {
	if f := d.follows[session]; f != nil {
		f.stream.Close()
	}
	delete(d.follows, session)
	delete(d.conns, session)
	delete(d.toAttach, session)
	delete(d.deltaThrough, session)
}

// Step does one pass of everything the daemon does: carries out ready effects, reads what the
// sidecars sent, and asks the job source when it is due. It reports whether anything changed, so a
// caller that wants to settle repeats it until it does not. A failure of one part does not stop the
// others; the first error is returned, and every part is retried by the next call.
func (d *Daemon) Step(ctx context.Context) (progress bool, err error) {
	var errs []error
	for _, part := range []func(context.Context) (bool, error){d.runEffects, d.pumpSidecars, d.pollJobs} {
		moved, err := part(ctx)
		progress = progress || moved
		if err != nil {
			errs = append(errs, err)
		}
	}
	return progress, errors.Join(errs...)
}

func (d *Daemon) runEffects(ctx context.Context) (bool, error) {
	pending := d.settle()
	claimed, err := d.Store.Claim(d.Clock.Now(), d.cfg.Batch)
	if err != nil {
		return false, errors.Join(pending, fmt.Errorf("claim effects: %w", err))
	}
	_, err = d.deliver(ctx, claimed)
	return len(claimed) > 0, errors.Join(pending, err)
}

// deliver carries out claimed effects in order and settles their outbox rows. An effect that fails
// holds back the rest of its session's effects, which are released unattempted: a later effect of a
// session never overtakes an earlier one that has to be retried. It returns the sessions whose
// launch is still to be carried out.
func (d *Daemon) deliver(ctx context.Context, claimed []store.Claimed) (launching map[core.Session]bool, err error) {
	launching = map[core.Session]bool{}
	held := map[core.Session]bool{}
	var errs []error
	for _, c := range claimed {
		session := effectSession(c.Effect)
		if session != (core.Session{}) && held[session] {
			d.retry = append(d.retry, c.ID)
			continue
		}
		if err := d.carryOut(ctx, c.Effect); err != nil {
			errs = append(errs, fmt.Errorf("effect %s: %w", c.Effect.ID(), err))
			d.retry = append(d.retry, c.ID)
			if session != (core.Session{}) {
				held[session] = true
				if _, isLaunch := c.Effect.(core.LaunchSession); isLaunch {
					launching[session] = true
				}
			}
			continue
		}
		d.done = append(d.done, c.ID)
	}
	return launching, errors.Join(append(errs, d.settle())...)
}

// settle tells the store which claimed effects are done and which to offer again. Rows it cannot
// settle stay listed and are settled by the next step: a row left claimed is never offered again
// until the store reopens.
func (d *Daemon) settle() error {
	var errs []error
	if len(d.done) > 0 {
		if err := d.Store.Complete(d.done...); err != nil {
			errs = append(errs, fmt.Errorf("complete effects: %w", err))
		} else {
			d.done = nil
		}
	}
	if len(d.retry) > 0 {
		if err := d.Store.Release(d.retry...); err != nil {
			errs = append(errs, fmt.Errorf("release effects: %w", err))
		} else {
			d.retry = nil
		}
	}
	return errors.Join(errs...)
}

// carryOut does one effect. A nil return means it need never be offered again; an error means try
// it again. A repeat of an effect the world already saw is harmless: sessions are launched once
// however often they are asked, and a sidecar answers a repeated effect id as it did the first time.
func (d *Daemon) carryOut(ctx context.Context, e core.Effect) error {
	state := d.State()
	switch b := e.(type) {
	case core.ArmTimer:
		// The store offers a timer only once it is due.
		_, err := d.Apply(core.TimerFired{Timer: b})
		return err
	case core.LaunchSession:
		if !isLive(state, b.Session) {
			return nil
		}
		return d.launch(ctx, b)
	}
	session := effectSession(e)
	if !isLive(state, session) {
		return nil // the session was replaced or ended, and the core accounted for what was in flight.
	}
	if cs, ok := e.(core.CloseSession); ok && cs.Force {
		sc, err := d.sidecar(ctx, session)
		if err != nil || sc == nil {
			return err
		}
		err = bounded(d.cfg.Bounds, ctx, sc.Kill)
		if err != nil && !errors.Is(err, ErrSidecarGone) {
			return fmt.Errorf("kill: %w", err)
		}
		d.forget(session)
		return nil
	}
	cmd, ok, err := wire.SidecarCommandFor(e)
	if err != nil || !ok {
		return err
	}
	sc, err := d.sidecar(ctx, session)
	if err != nil || sc == nil {
		return err
	}
	err = bounded(d.cfg.Bounds, ctx, func(c context.Context) error { return sc.Send(c, cmd) })
	if errors.Is(err, ErrSidecarGone) {
		return d.lose(session, "the session's sidecar went away while the daemon was sending it a command")
	}
	return err
}

func effectSession(e core.Effect) core.Session {
	switch b := e.(type) {
	case core.LaunchSession:
		return b.Session
	case core.StartTurn:
		return b.Session
	case core.Steer:
		return b.Session
	case core.Interrupt:
		return b.Session
	case core.CloseSession:
		return b.Session
	}
	return core.Session{}
}

func (d *Daemon) launch(ctx context.Context, l core.LaunchSession) error {
	if l.Session.N > 1 {
		// A resume replaces a session whose sidecar may still run: it ends before the new one starts.
		prev := core.Session{Agent: l.Session.Agent, N: l.Session.N - 1}
		if err := d.releaseUnattached(ctx, prev); err != nil {
			return err
		}
	}
	env := LaunchEnv{
		EnvSocket: d.cfg.AgentSocket, EnvAgent: string(l.Session.Agent), EnvSession: fmt.Sprint(l.Session.N),
		EnvToken: d.cfg.Authority.Token(l.Session), EnvRun: string(l.Run), EnvRole: string(l.Role),
	}
	if d.cfg.AgentCLI != "" {
		env[EnvCLI] = d.cfg.AgentCLI
	}
	sc, err := bound(d.cfg.Bounds, ctx, func(c context.Context) (Sidecar, error) {
		return d.cfg.Sidecars.Launch(c, l, env)
	})
	switch {
	case errors.Is(err, ErrExpired):
		// The launch may have started the process: asking again finds it, so this is a retry.
		return fmt.Errorf("launch %s/%d: %w", l.Session.Agent, l.Session.N, err)
	case err != nil:
		// A launch that fails is a session that is lost: the core resumes it a bounded number of
		// times and then gives up, where retrying here would never end. The reason goes with it.
		d.Log.Warn("launching a session failed", "agent", l.Session.Agent, "session", l.Session.N, "err", err)
		return d.lose(l.Session, fmt.Sprintf("the sidecar could not be launched: %v", err))
	}
	d.conns[l.Session] = sc
	delete(d.toAttach, l.Session)
	return nil
}

// pumpSidecars reads each live session's stream and feeds what it says to the core.
func (d *Daemon) pumpSidecars(ctx context.Context) (bool, error) {
	state := d.State()
	live := map[core.Session]bool{}
	for _, session := range liveSessions(state) {
		live[session] = true
	}
	var errs []error
	for session := range d.conns {
		if !live[session] {
			errs = append(errs, d.release(ctx, session))
		}
	}
	progress := false
	for _, session := range liveSessions(state) {
		moved, err := d.pump(ctx, session)
		progress = progress || moved
		if err != nil {
			errs = append(errs, err)
		}
	}
	return progress, errors.Join(errs...)
}

func (d *Daemon) pump(ctx context.Context, session core.Session) (bool, error) {
	sc, ok := d.conns[session]
	if !ok {
		if !d.toAttach[session] {
			return false, nil // the launch has not been carried out yet.
		}
		var err error
		if sc, err = d.sidecar(ctx, session); err != nil || sc == nil {
			return sc == nil && err == nil, err // a nil sidecar and no error is a session just lost.
		}
	}
	f := d.follows[session]
	if f == nil {
		offset := d.State().CommittedOffset(session)
		stream, err := bound(d.cfg.Bounds, ctx, func(c context.Context) (EventStream, error) {
			return sc.Follow(c, offset)
		})
		switch {
		case errors.Is(err, ErrSidecarGone):
			return true, d.lose(session, "the session's sidecar went away before its events could be read")
		case err != nil:
			return false, fmt.Errorf("follow %s/%d: %w", session.Agent, session.N, err)
		}
		f = &follow{stream: stream}
		d.follows[session] = f
	}
	progress := false
	for range d.cfg.Batch {
		data := f.stash
		if data == nil {
			ev, err := f.stream.Next()
			switch {
			case errors.Is(err, ErrIdle):
				return progress, nil
			case errors.Is(err, ErrSidecarGone):
				return true, d.lose(session, "the session's sidecar ended while the daemon was reading its events")
			case err != nil:
				// The stream broke; the next step reopens it after the committed offset.
				f.stream.Close()
				delete(d.follows, session)
				return progress, fmt.Errorf("follow %s/%d: %w", session.Agent, session.N, err)
			}
			raw, err := proto.Marshal(ev)
			if err != nil {
				return progress, fmt.Errorf("sidecar event of %s/%d: %w", session.Agent, session.N, err)
			}
			data = &eventStash{data: raw, offset: ev.GetOffset()}
		}
		fact, delta, err := wire.ParseSidecarEvent(session, data.data)
		switch {
		case err != nil:
			// An event the contract refuses can be neither recorded nor retried: say so and go on.
			d.Log.Warn("dropped an invalid sidecar event", "agent", session.Agent, "session", session.N, "err", err)
			f.stash = nil
			continue
		case delta != nil:
			// A stream reopened after the committed offset replays the deltas since it: show each once.
			if data.offset > d.deltaThrough[session] {
				d.deltaThrough[session] = data.offset
				d.Delta(delta)
			}
			f.stash = nil
			progress = true
			continue
		}
		if _, err := d.Apply(fact); err != nil {
			f.stash = data // nothing was consumed: try this event again next step.
			return progress, err
		}
		f.stash = nil
		progress = true
		if !isLive(d.State(), session) {
			return true, d.release(ctx, session)
		}
	}
	return progress, nil
}

func (d *Daemon) pollJobs(ctx context.Context) (bool, error) {
	now := d.Clock.Now()
	if d.cfg.Jobs == nil || (!d.polled.IsZero() && now.Sub(d.polled) < d.cfg.JobPollEvery) {
		return false, nil
	}
	d.polled = now
	var errs []error
	progress := false
	for _, run := range d.State().Runs() {
		if !run.Status.Live() {
			continue
		}
		updates, err := bound(d.cfg.Bounds, ctx, func(c context.Context) ([]core.JobUpdate, error) {
			return d.cfg.Jobs.Jobs(c, run)
		})
		if err != nil {
			// Nothing is known this time and the next poll says the same as this one would have.
			errs = append(errs, fmt.Errorf("jobs of run %s: %w", run.ID, err))
			continue
		}
		for _, u := range updates {
			applied, err := d.Apply(u)
			if err != nil {
				errs = append(errs, err)
				break
			}
			progress = progress || len(applied.Events) > 0
		}
	}
	return progress, errors.Join(errs...)
}

// Run steps on every wake and every tick until the context ends. ticks carries the merge window:
// each one ends it for the hub, and also steps the daemon, so a quiet daemon still polls its jobs
// and fires its timers. wake says a commit happened. The loop is the only caller of Step.
func (d *Daemon) Run(ctx context.Context, ticks <-chan time.Time, wake <-chan struct{}, log *slog.Logger) {
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticks:
			d.Hub.Flush()
		case <-wake:
		}
		// Settle: what one step did may be what the next one needs, such as a turn that ends and a
		// message that then starts another.
		for {
			moved, err := d.Step(ctx)
			if err != nil && ctx.Err() == nil {
				log.Warn("step failed; it will be retried", "err", err)
			}
			if !moved || err != nil || ctx.Err() != nil {
				break
			}
		}
	}
}
