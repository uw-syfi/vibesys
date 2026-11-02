package sim_test

import (
	"context"
	"errors"
	"fmt"
	"hash/fnv"
	"math/rand/v2"
	"net"
	"slices"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/metadata"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/app/memstores"
	"github.com/uw-syfi/vibesys/cli/internal/client"
	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/daemon"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv"
	"github.com/uw-syfi/vibesys/cli/internal/lifecycle"
	"github.com/uw-syfi/vibesys/cli/internal/owned"
	"github.com/uw-syfi/vibesys/cli/internal/runtimedir"
	"github.com/uw-syfi/vibesys/cli/internal/store"
	"github.com/uw-syfi/vibesys/cli/internal/supervisor"
)

const (
	actorKey = "sim-actor"
	version  = "sim"
)

type actorCtx struct{}

// actorOf names who makes a call, so that a call's label does not depend on when it arrived.
func actorOf(ctx context.Context) string {
	if a, ok := ctx.Value(actorCtx{}).(string); ok {
		return a
	}
	return "daemon"
}

// scopes are the stores of the simulation: two repositories, one of them with two tasks.
var scopes = []app.Scope{
	{Repository: "/repo-a", Task: "t"},
	{Repository: "/repo-a", Task: "u"},
	{Repository: "/repo-b", Task: "t"},
}

// world is what outlives one daemon, and the record of what happened.
type world struct {
	seed    uint64
	rng     *rand.Rand
	profile profile
	trace   strings.Builder
	group   owned.Group

	clock     *clock.Manual
	timers    *simTimers
	gate      gatekeeper
	gates     *simGates
	env       *fakeenv.Environment
	checkouts *fakeenv.Checkouts
	survivors *supervisor.Survivors
	rules     store.Rules
	raw       map[app.Scope]store.Store

	mu        sync.Mutex
	daemon    *incarnation
	next      int
	startHeld chan struct{}
	conns     []net.Conn
	follows   map[lifecycle.Ref]context.CancelCauseFunc
	absent    map[string]bool

	// What happened, for the invariants.
	killed     map[lifecycle.Ref]bool
	spawned    []lifecycle.Ref
	asked      map[lifecycle.Ref]bool
	finals     map[lifecycle.Ref]core.JobState
	flaky      map[lifecycle.Ref]bool
	cancelSeen map[lifecycle.Ref]*incarnation
	cancelSent map[lifecycle.Ref]bool
	probes     map[lifecycle.Ref]int
	// unanswered are the tokens of submissions whose caller got an error: the job may be in its
	// store, and the daemon then finds it out of its turn.
	unanswered map[string]bool
	// submitBegin and submitEnd say when a submission began and was answered, as numbers in one
	// sequence.
	submitBegin, submitEnd map[string]int
	submitTick             int
	appendedAt             map[lifecycle.Ref]time.Duration
	appends                map[lifecycle.Ref]int
	violation              string

	ops     []*op
	opCount int
	tokens  map[string]*script
	stop    context.CancelFunc
	ctx     context.Context
}

// script is what one submitted command prints and how it exits.
type script struct {
	token string
	lines []string
	code  int
	gates []string
}

func (s *script) argv() []string {
	var out []string
	for i, line := range s.lines {
		out = append(out, "print", line, ";")
		if i < len(s.gates) {
			out = append(out, "hold", s.gates[i], ";")
		}
	}
	return append(out, "exit", fmt.Sprint(s.code))
}

func (s *script) output() string { return strings.Join(s.lines, "\n") + "\n" }

func newWorld(seed uint64) *world {
	rng := rand.New(rand.NewPCG(seed, seed^0x9e3779b97f4a7c15))
	c := clock.NewManual(time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC), 0)
	gates := newSimGates()
	ctx, stop := context.WithCancel(context.Background())
	w := &world{
		rng: rng, profile: profileOf(rng),
		clock: c, timers: newSimTimers(c), gates: gates,
		env:       fakeenv.New(core.EnvironmentHost, gates),
		checkouts: &fakeenv.Checkouts{},
		survivors: supervisor.NewSurvivors(),
		rules:     app.Rules(5),
		raw:       map[app.Scope]store.Store{},
		startHeld: make(chan struct{}, 1),
		follows:   map[lifecycle.Ref]context.CancelCauseFunc{},
		absent:    map[string]bool{},
		killed:    map[lifecycle.Ref]bool{}, asked: map[lifecycle.Ref]bool{},
		finals: map[lifecycle.Ref]core.JobState{}, flaky: map[lifecycle.Ref]bool{},
		cancelSeen: map[lifecycle.Ref]*incarnation{}, cancelSent: map[lifecycle.Ref]bool{},
		probes: map[lifecycle.Ref]int{}, unanswered: map[string]bool{}, seed: seed,
		submitBegin: map[string]int{}, submitEnd: map[string]int{},
		appendedAt: map[lifecycle.Ref]time.Duration{}, appends: map[lifecycle.Ref]int{},
		tokens: map[string]*script{},
		ctx:    ctx, stop: stop,
	}
	mem := memstores.New(w.rules)
	for _, s := range scopes {
		st, err := mem.Open(context.Background(), s)
		if err != nil {
			panic(err)
		}
		w.raw[s] = st
	}
	return w
}

func (w *world) tracef(format string, args ...any) {
	fmt.Fprintf(&w.trace, format+"\n", args...)
}

func (w *world) violate(format string, args ...any) {
	if w.violation == "" {
		w.violation = fmt.Sprintf(format, args...)
		w.tracef("VIOLATION: %s", w.violation)
	}
}

// simStores is the stores as one daemon reaches them.
type simStores struct {
	w *world
	d *incarnation
}

var _ app.Stores = simStores{}

func (s simStores) Open(ctx context.Context, scope app.Scope) (store.Store, error) {
	if err := scope.Validate(); err != nil {
		return nil, err
	}
	return gated(ctx, s.w, s.d, "stores.Open "+scope.Repository+" "+scope.Task, false, func() (store.Store, error) {
		st, ok := s.w.raw[scope]
		if !ok {
			return nil, &core.Failure{Kind: core.FailureInvalidInput, Message: "no such store in the simulation"}
		}
		if s.w.isAbsent(scope.Repository) {
			return nil, &core.Failure{Kind: core.FailureUnavailable, Message: "the repository is not there"}
		}
		return gatedStore{Store: st, scope: scope, w: s.w, d: s.d}, nil
	})
}

// Known is as filestores.Known behaves: a repository that is away stays known.
func (s simStores) Known(ctx context.Context) ([]app.Scope, error) {
	return gated(ctx, s.w, s.d, "stores.Known", false, func() ([]app.Scope, error) { return slices.Clone(scopes), nil })
}

func (w *world) isAbsent(repo string) bool {
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.absent[repo]
}

// incarnation is one daemon process.
type incarnation struct {
	id      int
	dead    atomic.Bool
	dying   chan struct{}
	stopped chan struct{}
	died    chan struct{}
	app     *app.App
	server  *daemon.Server
	rpc     *grpc.Server
	lis     *pipeListener
	serving chan struct{}
	exited  chan struct{}
}

// Start implements client.Starter: one daemon process, up once it serves.
func (w *world) Start(ctx context.Context) error {
	w.mu.Lock()
	if w.daemon != nil {
		w.mu.Unlock()
		return runtimedir.ErrDaemonRunning
	}
	w.next++
	d := &incarnation{
		id: w.next, dying: make(chan struct{}), stopped: make(chan struct{}), died: make(chan struct{}),
		serving: make(chan struct{}), exited: make(chan struct{}),
	}
	w.daemon = d
	w.mu.Unlock()
	w.tracef("daemon %d starts", d.id)

	d.app = app.New(app.Config{
		Clock: w.clock, Timers: w.timers, Stores: simStores{w, d},
		Environments: fakeenv.Environments{core.EnvironmentHost: w.env},
		Supervisors:  gatedSupervisors{supervisor.NewInProcess(fakeenv.Environments{core.EnvironmentHost: w.env}, w.checkouts, w.survivors), w, d},
	})
	w.group.Go(func() { w.process(d) })
	select {
	case <-d.serving:
		return nil
	case <-d.exited:
		return &core.Failure{Kind: core.FailureUnavailable, Message: "the daemon did not start"}
	case <-ctx.Done():
		return ctx.Err()
	}
}

// process is the life of a daemon, as daemon.Run has it.
func (w *world) process(d *incarnation) {
	defer func() {
		d.app.Close()
		w.mu.Lock()
		if w.daemon == d {
			w.daemon = nil
		}
		w.mu.Unlock()
		close(d.exited)
	}()
	if err := d.app.Start(context.WithValue(w.ctx, actorCtx{}, fmt.Sprintf("start%d", d.id))); err != nil {
		w.tracef("daemon %d did not start: %v", d.id, err)
		return
	}
	if d.dead.Load() {
		return
	}
	d.server = daemon.NewServer(d.app, version)
	d.rpc = grpc.NewServer(grpc.ChainUnaryInterceptor(unaryActor), grpc.ChainStreamInterceptor(streamActor))
	cliv1.RegisterDaemonServiceServer(d.rpc, d.server)
	d.lis = newPipeListener()
	var served owned.Group
	served.Go(func() { _ = d.rpc.Serve(d.lis) })
	defer served.Wait()
	close(d.serving)
	defer close(d.stopped)
	graceful := make(chan struct{})
	served.Go(func() {
		select {
		case <-d.server.Stopping():
			d.rpc.GracefulStop()
		case <-d.dying:
		}
		close(graceful)
	})
	select {
	case <-graceful:
	case <-d.dying:
	}
	select {
	case <-d.dying:
		d.rpc.Stop()
		d.server.Shutdown()
	default:
	}
}

func actorFrom(ctx context.Context) context.Context {
	md, _ := metadata.FromIncomingContext(ctx)
	if v := md.Get(actorKey); len(v) > 0 {
		return context.WithValue(ctx, actorCtx{}, v[0])
	}
	return ctx
}

func unaryActor(ctx context.Context, req any, _ *grpc.UnaryServerInfo, next grpc.UnaryHandler) (any, error) {
	return next(actorFrom(ctx), req)
}

type actorStream struct {
	grpc.ServerStream
	ctx context.Context
}

func (s actorStream) Context() context.Context { return s.ctx }

func streamActor(srv any, ss grpc.ServerStream, _ *grpc.StreamServerInfo, next grpc.StreamHandler) error {
	return next(srv, actorStream{ss, actorFrom(ss.Context())})
}

// The Rendezvous of the simulation.
type rendezvous struct{ w *world }

var _ client.Rendezvous = rendezvous{}

var errRefused = errors.New("connection refused")

// pipeListener serves gRPC in memory over net.Pipe. It is not grpc's bufconn because a bufconn makes
// zero-length timers, and on Go 1.25 a zero-length timer in a synctest bubble crashes the race
// detector (the runtime runs it on the system stack, which has no race context).
type pipeListener struct {
	conns  chan net.Conn
	closed chan struct{}
	once   sync.Once
}

func newPipeListener() *pipeListener {
	return &pipeListener{conns: make(chan net.Conn), closed: make(chan struct{})}
}

func (l *pipeListener) Accept() (net.Conn, error) {
	select {
	case c := <-l.conns:
		return c, nil
	case <-l.closed:
		return nil, net.ErrClosed
	}
}

func (l *pipeListener) Close() error {
	l.once.Do(func() { close(l.closed) })
	return nil
}

func (l *pipeListener) Addr() net.Addr { return pipeAddr{} }

func (l *pipeListener) dial(ctx context.Context) (net.Conn, error) {
	near, far := net.Pipe()
	select {
	case l.conns <- far:
		return near, nil
	case <-ctx.Done():
		return nil, ctx.Err()
	case <-l.closed:
		return nil, net.ErrClosed
	}
}

type pipeAddr struct{}

func (pipeAddr) Network() string { return "pipe" }
func (pipeAddr) String() string  { return "pipe" }

func (r rendezvous) Dial(ctx context.Context) (net.Conn, error) {
	r.w.mu.Lock()
	d := r.w.daemon
	r.w.mu.Unlock()
	if d == nil || d.dead.Load() {
		return nil, errRefused
	}
	select {
	case <-d.serving:
	default:
		return nil, errRefused
	}
	conn, err := d.lis.dial(ctx)
	if err != nil {
		return nil, errRefused
	}
	r.w.mu.Lock()
	r.w.conns = append(r.w.conns, conn)
	r.w.mu.Unlock()
	return conn, nil
}

func (r rendezvous) HoldStart(ctx context.Context) (func() error, error) {
	select {
	case r.w.startHeld <- struct{}{}:
		return func() error { <-r.w.startHeld; return nil }, nil
	case <-ctx.Done():
		return nil, ctx.Err()
	}
}

func (r rendezvous) AwaitExit(ctx context.Context) error {
	r.w.mu.Lock()
	d := r.w.daemon
	r.w.mu.Unlock()
	if d == nil {
		return nil
	}
	select {
	case <-d.exited:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

func (w *world) options() client.Options {
	return client.Options{Rendezvous: rendezvous{w}, Version: version, Starter: w, Timers: w.timers}
}

// crash ends the daemon at once, as a kill does.
func (w *world) crash() {
	w.mu.Lock()
	d := w.daemon
	w.mu.Unlock()
	if d == nil {
		return
	}
	w.tracef("daemon %d dies", d.id)
	d.dead.Store(true)
	close(d.dying)
	// What a dead daemon was doing never answers anyone: its connections go first.
	select {
	case <-d.serving:
		<-d.stopped
	default:
	}
	close(d.died)
	w.gate.dropDaemon(d)
	d.app.Close()
	<-d.exited
}

// The records the Fakes keep.

func (w *world) refIn(scope app.Scope, id core.JobID) lifecycle.Ref {
	return lifecycle.Ref{Store: lifecycle.StoreIDOf(scope.Repository, scope.Task), Job: id}
}

func (w *world) addFollow(ref lifecycle.Ref, sever context.CancelCauseFunc) {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.follows[ref] = sever
}

func (w *world) dropFollow(ref lifecycle.Ref) {
	w.mu.Lock()
	defer w.mu.Unlock()
	delete(w.follows, ref)
}

func (w *world) noteSpawn(ref lifecycle.Ref) {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.spawned = append(w.spawned, ref)
}

// noteCancelRead records a cancel call that found its job queued: the daemon has the cancel now.
func (w *world) noteCancelRead(d *incarnation, scope app.Scope, j core.Job) {
	if j.State != core.JobQueued {
		return
	}
	w.mu.Lock()
	defer w.mu.Unlock()
	if _, ok := w.cancelSeen[w.refIn(scope, j.ID)]; !ok {
		w.cancelSeen[w.refIn(scope, j.ID)] = d
	}
}

// noteCancelSent records a cancel that reached a supervisor, which then never runs its command.
func (w *world) noteCancelSent(ref lifecycle.Ref, reason string) {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.cancelSent[ref] = true
	if strings.HasPrefix(reason, lifecycle.StopReason) {
		// The daemon stopped the job, and a later daemon finds a supervisor that was cancelled.
		// The reason says who did.
		w.asked[ref] = true
	}
}

// noteSubmit records that a submission began or was answered.
func (w *world) noteSubmit(token string, answered bool) {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.submitTick++
	if answered {
		w.submitEnd[token] = w.submitTick
	} else {
		w.submitBegin[token] = w.submitTick
	}
}

// noteStart checks no command starts after the daemon took its job's cancel.
func (w *world) noteStart(d *incarnation, ref lifecycle.Ref) {
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.cancelSeen[ref] == d && !w.cancelSent[ref] {
		w.violate("job %s started after daemon %d took its cancel while it was queued", ref, d.id)
	}
}

// noteAppend counts appends that came with no time between them: output that arrives steadily is
// batched and not stored line by line.
func (w *world) noteAppend(scope app.Scope, id core.JobID) {
	ref := w.refIn(scope, id)
	w.timers.mu.Lock()
	now := w.timers.now
	w.timers.mu.Unlock()
	w.mu.Lock()
	defer w.mu.Unlock()
	if at, ok := w.appendedAt[ref]; ok && at == now {
		w.appends[ref]++
	} else {
		w.appends[ref] = 1
	}
	w.appendedAt[ref] = now
}

// noteRelease checks a supervisor is let go only when its job's end is safe: nothing else knows
// how the command ended.
func (w *world) noteRelease(ref lifecycle.Ref) {
	for _, scope := range scopes {
		if w.refIn(scope, ref.Job) != ref {
			continue
		}
		j, err := w.raw[scope].Job(context.Background(), ref.Job)
		if err == nil && j.State == core.JobRunning {
			w.violate("the supervisor of %s was let go while its job is running", ref)
		}
	}
}

// probed is what a daemon learns of a run. A live supervisor may not answer, which tells the
// daemon nothing about whether it lives. Which probes fail comes from the seed and the probe's
// number for that run, as probes run beside each other and a shared generator would make their
// order matter.
func (w *world) probed(r supervisor.Run) supervisor.Run {
	if r.Presence != lifecycle.PresenceAlive || !w.flakyProbe(r.Ref) {
		return r
	}
	return supervisor.Run{Ref: r.Ref, Presence: lifecycle.PresenceUnknown, Launch: r.Launch}
}

func (w *world) flakyProbe(ref lifecycle.Ref) bool {
	if !w.profile.probes {
		return false
	}
	w.mu.Lock()
	defer w.mu.Unlock()
	w.probes[ref]++
	h := fnv.New64a()
	fmt.Fprintf(h, "%d %s %d", w.seed, ref, w.probes[ref])
	return h.Sum64()%3 == 0
}

// jobs returns every job of every store, as the disk has it, in a fixed order.
func (w *world) jobs() []located {
	var out []located
	for _, scope := range scopes {
		list, err := w.raw[scope].Jobs(context.Background(), core.JobFilter{})
		if err != nil {
			panic(err)
		}
		for _, j := range list {
			out = append(out, located{scope, j})
		}
	}
	return out
}

type located struct {
	scope app.Scope
	job   core.Job
}

func tokenOf(j core.Job) string {
	if c, ok := j.Spec.Settings.(core.CommandSettings); ok && len(c.Argv) > 1 {
		return strings.SplitN(c.Argv[1], "-", 2)[0]
	}
	return ""
}

func (w *world) outputOf(l located) string {
	var out []byte
	var offset uint64
	for {
		chunk, err := w.raw[l.scope].ReadLog(context.Background(), l.job.ID, offset, 1<<20)
		if err != nil {
			panic(err)
		}
		if len(chunk.Data) == 0 {
			return string(out)
		}
		out = append(out, chunk.Data...)
		offset = chunk.Next
	}
}

// startsOf returns the tokens of the commands that ran, in order.
func (w *world) startsOf() []string {
	var out []string
	for _, x := range w.env.Runs() {
		if len(x.Argv) > 1 {
			out = append(out, strings.SplitN(x.Argv[1], "-", 2)[0])
		}
	}
	return out
}

func sortedKeys[V any](m map[string]V) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	slices.Sort(out)
	return out
}
