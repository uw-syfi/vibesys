package daemon

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"time"

	"google.golang.org/protobuf/proto"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/jobs"
	"github.com/uw-syfi/vibesys/orchestrator/internal/store"
	"github.com/uw-syfi/vibesys/orchestrator/internal/wire"
)

// Config is what a Daemon is made of.
type Config struct {
	Engine    EngineDeps
	Sidecars  Sidecars
	Jobs      jobs.JobSource
	Authority Authority
	// Socket is the path of the daemon's socket, told to every session it launches.
	Socket string
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
}

// follow is one open Follow, and the event that could not be committed yet.
type follow struct {
	stream EventStream
	stash  *eventStash
}

type eventStash struct {
	data []byte
}

// Open recovers the state and returns a daemon. Call Reattach before the first Step.
func Open(cfg Config) (*Daemon, error) {
	if cfg.Batch <= 0 {
		cfg.Batch = 64
	}
	engine, err := OpenEngine(cfg.Engine)
	if err != nil {
		return nil, err
	}
	return &Daemon{
		Engine: engine, cfg: cfg,
		conns: map[core.Session]Sidecar{}, follows: map[core.Session]*follow{},
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

func isLive(s core.State, session core.Session) bool {
	a, ok := s.Agent(session.Agent)
	return ok && a.Session == session.N && !a.Status.Over()
}

// Reattach finds the sidecars that outlived the previous daemon. A session whose sidecar is gone is
// lost, which the core answers by resuming it or giving up. It is what a restart does besides
// recovering the state, and it is safe to repeat.
func (d *Daemon) Reattach(ctx context.Context) error {
	for _, session := range liveSessions(d.State()) {
		if _, err := d.sidecar(ctx, session); err != nil {
			return err
		}
	}
	return nil
}

// sidecar returns the connection to a live session's sidecar, attaching it if this daemon has not
// met it. A session whose sidecar is gone is reported lost and returns a nil Sidecar and no error.
func (d *Daemon) sidecar(ctx context.Context, session core.Session) (Sidecar, error) {
	if sc, ok := d.conns[session]; ok {
		return sc, nil
	}
	sc, err := d.cfg.Sidecars.Attach(ctx, session)
	switch {
	case errors.Is(err, ErrSidecarGone):
		return nil, d.lose(session)
	case err != nil:
		return nil, fmt.Errorf("attach %s/%d: %w", session.Agent, session.N, err)
	}
	d.conns[session] = sc
	return sc, nil
}

// lose reports a session's sidecar gone and forgets what the daemon held for it.
func (d *Daemon) lose(session core.Session) error {
	d.forget(session)
	_, err := d.Apply(core.SidecarLost{Session: session})
	return err
}

func (d *Daemon) forget(session core.Session) {
	if f := d.follows[session]; f != nil {
		f.stream.Close()
	}
	delete(d.follows, session)
	delete(d.conns, session)
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
	claimed, err := d.Store.Claim(d.Clock.Now(), d.cfg.Batch)
	if err != nil {
		return false, fmt.Errorf("claim effects: %w", err)
	}
	var done, retry []store.OutboxID
	var errs []error
	for _, c := range claimed {
		if err := d.carryOut(ctx, c.Effect); err != nil {
			errs = append(errs, fmt.Errorf("effect %s: %w", c.Effect.ID(), err))
			retry = append(retry, c.ID)
			continue
		}
		done = append(done, c.ID)
	}
	if len(done) > 0 {
		if err := d.Store.Complete(done...); err != nil {
			errs = append(errs, fmt.Errorf("complete effects: %w", err))
		}
	}
	if len(retry) > 0 {
		if err := d.Store.Release(retry...); err != nil {
			errs = append(errs, fmt.Errorf("release effects: %w", err))
		}
	}
	return len(claimed) > 0, errors.Join(errs...)
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
		if sc, err := d.sidecar(ctx, session); err != nil || sc == nil {
			return err
		} else if err := sc.Kill(ctx); err != nil && !errors.Is(err, ErrSidecarGone) {
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
	switch err := sc.Send(ctx, cmd); {
	case errors.Is(err, ErrSidecarGone):
		return d.lose(session)
	default:
		return err
	}
}

func effectSession(e core.Effect) core.Session {
	switch b := e.(type) {
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
	env := LaunchEnv{
		EnvSocket: d.cfg.Socket, EnvAgent: string(l.Session.Agent), EnvSession: fmt.Sprint(l.Session.N),
		EnvToken: d.cfg.Authority.Token(l.Session), EnvRun: string(l.Run), EnvRole: string(l.Role),
	}
	sc, err := d.cfg.Sidecars.Launch(ctx, l, env)
	if err != nil {
		// A launch that fails is a session that is lost: the core resumes it a bounded number of
		// times and then gives up, where retrying here would never end.
		d.Log.Warn("launching a session failed", "agent", l.Session.Agent, "session", l.Session.N, "err", err)
		return d.lose(l.Session)
	}
	d.conns[l.Session] = sc
	return nil
}

// pumpSidecars reads each live session's stream and feeds what it says to the core.
func (d *Daemon) pumpSidecars(ctx context.Context) (bool, error) {
	state := d.State()
	live := map[core.Session]bool{}
	for _, session := range liveSessions(state) {
		live[session] = true
	}
	for session := range d.conns {
		if !live[session] {
			d.forget(session)
		}
	}
	var errs []error
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
		return false, nil // the launch has not been carried out yet.
	}
	f := d.follows[session]
	if f == nil {
		stream, err := sc.Follow(ctx, d.State().CommittedOffset(session))
		switch {
		case errors.Is(err, ErrSidecarGone):
			return true, d.lose(session)
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
				return true, d.lose(session)
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
			data = &eventStash{data: raw}
		}
		fact, delta, err := wire.ParseSidecarEvent(session, data.data)
		switch {
		case err != nil:
			// An event the contract refuses can be neither recorded nor retried: say so and go on.
			d.Log.Warn("dropped an invalid sidecar event", "agent", session.Agent, "session", session.N, "err", err)
			f.stash = nil
			continue
		case delta != nil:
			d.Delta(delta)
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
			d.forget(session)
			return true, nil
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
		updates, err := d.cfg.Jobs.Jobs(ctx, run)
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
