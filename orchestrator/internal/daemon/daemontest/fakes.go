// Package daemontest holds the Fakes a daemon is tested against: a clock, nonces, a sidecar host
// whose sessions keep the sidecar contract, and a plan reader.
package daemontest

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"time"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
)

// Clock is a clock a test moves.
type Clock struct {
	mu  sync.Mutex
	now time.Time
}

// NewClock returns a clock at a fixed instant.
func NewClock() *Clock { return &Clock{now: time.Date(2026, 1, 2, 3, 4, 5, 0, time.UTC)} }

// Now implements daemon.Clock.
func (c *Clock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.now
}

// Advance moves the clock forward.
func (c *Clock) Advance(d time.Duration) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.now = c.now.Add(d)
}

// Nonces counts, and keeps counting across the daemons of one test, as random nonces never repeat.
type Nonces struct {
	mu sync.Mutex
	n  uint64
}

// Next implements daemon.Nonces.
func (n *Nonces) Next() core.Nonce {
	n.mu.Lock()
	defer n.mu.Unlock()
	n.n++
	return core.Nonce(fmt.Sprintf("n%06d", n.n))
}

// Plans is a PlanReader over plans a test sets.
type Plans struct {
	mu    sync.Mutex
	plans map[core.RunID][]*pb.Plan
	// Stall makes that many calls hang until their context ends, calling Elapse first.
	Stall  int
	Elapse func()
}

func (p *Plans) hang(ctx context.Context) (bool, error) {
	p.mu.Lock()
	stall := p.Stall > 0
	if stall {
		p.Stall--
	}
	elapse := p.Elapse
	p.mu.Unlock()
	if !stall {
		return false, nil
	}
	if ctx.Done() == nil {
		return true, errors.New("fake plans: a call that hangs was made with no bound")
	}
	if elapse != nil {
		elapse()
	}
	<-ctx.Done()
	return true, ctx.Err()
}

// NewPlans returns a reader with no plans.
func NewPlans() *Plans { return &Plans{plans: map[core.RunID][]*pb.Plan{}} }

// Set replaces a run's plans.
func (p *Plans) Set(run core.RunID, plans ...*pb.Plan) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.plans[run] = plans
}

// ListPlans implements daemon.PlanReader.
func (p *Plans) ListPlans(ctx context.Context, run core.Run) ([]*pb.Plan, error) {
	if hung, err := p.hang(ctx); hung {
		return nil, err
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.plans[run.ID], nil
}

// ViewPlan implements daemon.PlanReader.
func (p *Plans) ViewPlan(_ context.Context, run core.Run, number uint32) (*pb.Plan, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	for _, plan := range p.plans[run.ID] {
		if plan.GetNumber() == number {
			return plan, nil
		}
	}
	return nil, daemon.ErrPlanNotFound
}

// Reaction is what a session does when it is sent a command. The default one is DefaultReaction.
type Reaction func(s *Session, cmd *pb.SidecarCommand)

// Sidecars is a host of fake sidecars. A Session lives in it until it dies or closes, however many
// daemons come and go, as a real sidecar outlives the daemon.
type Sidecars struct {
	mu       sync.Mutex
	sessions map[core.Session]*Session
	// React decides what each command does. Nil means DefaultReaction.
	React Reaction
	// LaunchFailures makes that many launches fail before any succeeds.
	LaunchFailures int
	// Wake is called when a session emits events on its own, as a real stream's goroutine wakes the
	// daemon. A Rig sets it.
	Wake func()
	// AttachFails makes Attach report every sidecar gone, as if a host restart had killed them.
	AttachFails bool
	// Stall makes that many calls (of Launch, Attach, Send, Follow, Kill) hang until their context
	// ends, as a sidecar that has stopped answering would. Elapse is called when a call hangs, and
	// stands for the time passing until the caller's bound ends: a Rig sets it to its Bounds. A
	// call that hangs with a context nothing can end is reported as Unbounded and fails at once,
	// so a daemon that forgot its bound fails a test and does not hang it.
	Stall     int
	Elapse    func()
	Unbounded int
	// LaunchError, when set, is what a failing launch reports.
	LaunchError error
	// KillFailures makes that many Kill calls fail before any succeeds, as a signal that cannot be
	// delivered does.
	KillFailures int
	// AfterSend, when set, is called after each command a session took, with the session's lock
	// released. A test crashes the process from it.
	AfterSend func()
}

// hang blocks until the call's context ends, if a stall is pending. It returns whether it did.
func (h *Sidecars) hang(ctx context.Context) (bool, error) {
	h.mu.Lock()
	stall := h.Stall > 0
	if stall {
		h.Stall--
	}
	elapse := h.Elapse
	h.mu.Unlock()
	if !stall {
		return false, nil
	}
	if ctx.Done() == nil {
		h.mu.Lock()
		h.Unbounded++
		h.mu.Unlock()
		return true, errors.New("fake host: a call that hangs was made with no bound")
	}
	if elapse != nil {
		elapse()
	}
	<-ctx.Done()
	return true, ctx.Err()
}

// NewSidecars returns a host with no sessions.
func NewSidecars() *Sidecars { return &Sidecars{sessions: map[core.Session]*Session{}} }

// Session returns a session, or nil.
func (h *Sidecars) Session(s core.Session) *Session {
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.sessions[s]
}

// Sessions returns every session ever launched.
func (h *Sidecars) Sessions() []*Session {
	h.mu.Lock()
	defer h.mu.Unlock()
	out := make([]*Session, 0, len(h.sessions))
	for _, s := range h.sessions {
		out = append(out, s)
	}
	return out
}

// Launch implements daemon.Sidecars. A repeated launch finds the session it started.
func (h *Sidecars) Launch(ctx context.Context, l core.LaunchSession, env daemon.LaunchEnv) (daemon.Sidecar, error) {
	if hung, err := h.hang(ctx); hung {
		return nil, err
	}
	h.mu.Lock()
	defer h.mu.Unlock()
	if s, ok := h.sessions[l.Session]; ok {
		return &conn{host: h, s: s}, nil
	}
	if h.LaunchFailures > 0 {
		h.LaunchFailures--
		if h.LaunchError != nil {
			return nil, h.LaunchError
		}
		return nil, fmt.Errorf("fake host: cannot start %s/%d", l.Session.Agent, l.Session.N)
	}
	s := &Session{Launch: l, Env: env, seen: map[string]bool{}, host: h}
	h.sessions[l.Session] = s
	conversation := l.ConversationID
	if conversation == "" {
		conversation = fmt.Sprintf("conv-%s", l.Session.Agent)
	}
	s.Emit(&pb.SidecarEvent{Body: &pb.SidecarEvent_Opened{Opened: &pb.ConversationOpened{
		ProviderConversationId: conversation, Worktree: "/work/" + string(l.Session.Agent),
		Capabilities: &pb.Capabilities{StreamsText: true, Steers: true, Resumes: true},
	}}})
	return &conn{host: h, s: s}, nil
}

// Attach implements daemon.Sidecars.
func (h *Sidecars) Attach(ctx context.Context, session core.Session) (daemon.Sidecar, error) {
	if hung, err := h.hang(ctx); hung {
		return nil, err
	}
	h.mu.Lock()
	defer h.mu.Unlock()
	s, ok := h.sessions[session]
	if !ok || s.dead || h.AttachFails {
		return nil, daemon.ErrSidecarGone
	}
	return &conn{host: h, s: s}, nil
}

// Session is one fake sidecar: the events it has emitted, and the commands it has taken.
type Session struct {
	Launch core.LaunchSession
	Env    daemon.LaunchEnv
	// Received lists each command once, by effect id.
	Received []*pb.SidecarCommand

	mu     sync.Mutex
	events []*pb.SidecarEvent
	seen   map[string]bool
	dead   bool
	closed bool
	// Held, when set, keeps the session from reacting: a test emits events itself.
	Held bool
	// failNext makes the next read of the stream report a break that a reopened stream recovers from.
	failNext bool
	host     *Sidecars
}

// Emit appends an event to the session's stream, giving it the next offset.
func (s *Session) Emit(ev *pb.SidecarEvent) {
	if ev.Offset == 0 {
		ev.Offset = uint64(len(s.events)) + 1
	}
	s.events = append(s.events, ev)
}

// EmitEvents appends events under the session's lock.
func (s *Session) EmitEvents(evs ...*pb.SidecarEvent) {
	s.mu.Lock()
	for _, ev := range evs {
		s.Emit(ev)
	}
	s.mu.Unlock()
	if s.host.Wake != nil {
		s.host.Wake()
	}
}

// Die ends the process with no word to anyone: its stream fails and Attach finds nothing.
func (s *Session) Die() {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.dead = true
}

// BreakStream makes the next read of any stream of the session fail, as a dropped connection does.
// The session and its events are untouched, and a stream opened afterwards reads them again.
func (s *Session) BreakStream() {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.failNext = true
}

// Commands returns the commands the session took, each once.
func (s *Session) Commands() []*pb.SidecarCommand {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]*pb.SidecarCommand(nil), s.Received...)
}

// Dead says the process is gone.
func (s *Session) Dead() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.dead
}

type conn struct {
	host *Sidecars
	s    *Session
}

func (c *conn) Send(ctx context.Context, cmd *pb.SidecarCommand) error {
	if hung, err := c.host.hang(ctx); hung {
		return err
	}
	err := c.send(cmd)
	if c.host.AfterSend != nil {
		c.host.AfterSend()
	}
	return err
}

func (c *conn) send(cmd *pb.SidecarCommand) error {
	c.s.mu.Lock()
	defer c.s.mu.Unlock()
	if c.s.dead {
		return daemon.ErrSidecarGone
	}
	if c.s.seen[cmd.GetEffectId()] {
		return nil // a repeated effect id answers as the first delivery did.
	}
	c.s.seen[cmd.GetEffectId()] = true
	c.s.Received = append(c.s.Received, cmd)
	if c.s.Held {
		return nil
	}
	react := c.host.React
	if react == nil {
		react = DefaultReaction
	}
	react(c.s, cmd)
	return nil
}

func (c *conn) Follow(ctx context.Context, after uint64) (daemon.EventStream, error) {
	if hung, err := c.host.hang(ctx); hung {
		return nil, err
	}
	c.s.mu.Lock()
	defer c.s.mu.Unlock()
	if c.s.dead {
		return nil, daemon.ErrSidecarGone
	}
	return &stream{s: c.s, after: after}, nil
}

func (c *conn) Kill(ctx context.Context) error {
	if hung, err := c.host.hang(ctx); hung {
		return err
	}
	c.host.mu.Lock()
	failing := c.host.KillFailures > 0
	if failing {
		c.host.KillFailures--
	}
	c.host.mu.Unlock()
	if failing {
		return ErrInjected
	}
	c.s.mu.Lock()
	defer c.s.mu.Unlock()
	c.s.dead = true
	return nil
}

type stream struct {
	s     *Session
	after uint64
}

func (st *stream) Next() (*pb.SidecarEvent, error) {
	st.s.mu.Lock()
	defer st.s.mu.Unlock()
	if st.s.failNext {
		st.s.failNext = false
		return nil, errors.New("fake stream: the connection dropped")
	}
	for _, ev := range st.s.events {
		if ev.Offset > st.after {
			st.after = ev.Offset
			return ev, nil
		}
	}
	if st.s.dead {
		return nil, daemon.ErrSidecarGone
	}
	return nil, daemon.ErrIdle
}

func (st *stream) Close() {}

// DefaultReaction makes a session behave like a cooperative agent: a turn writes one item and
// ends, a steer is accepted, an interrupt ends the turn, and a close ends the session.
func DefaultReaction(s *Session, cmd *pb.SidecarCommand) {
	switch b := cmd.GetBody().(type) {
	case *pb.SidecarCommand_StartTurn:
		AnswerTurn(s, b.StartTurn.GetTurnId(), "ok")
	case *pb.SidecarCommand_Steer:
		s.Emit(&pb.SidecarEvent{Body: &pb.SidecarEvent_Steer{Steer: &pb.SteerOutcome{
			MessageId: b.Steer.GetMessage().GetId(), Accepted: true,
		}}})
	case *pb.SidecarCommand_Interrupt:
		s.Emit(&pb.SidecarEvent{Body: &pb.SidecarEvent_TurnEnded{TurnEnded: &pb.TurnEnded{
			TurnId: b.Interrupt.GetTurnId(), Status: pb.TurnStatus_TURN_STATUS_INTERRUPTED,
		}}})
	case *pb.SidecarCommand_Close:
		s.closed = true
		s.Emit(&pb.SidecarEvent{Body: &pb.SidecarEvent_Closed{Closed: &pb.SessionClosed{}}})
	}
}

// AnswerTurn emits what a turn that writes `text` and ends looks like: a delta, the finished item,
// and the end of the turn. The session's lock must be held.
func AnswerTurn(s *Session, turn, text string) {
	item := "item-" + turn
	s.Emit(&pb.SidecarEvent{Body: &pb.SidecarEvent_Delta{Delta: &pb.SidecarDelta{
		ItemId: item, Body: &pb.SidecarDelta_Text{Text: text},
	}}})
	s.Emit(&pb.SidecarEvent{Body: &pb.SidecarEvent_Item{Item: &pb.SidecarItem{
		Id: item, TurnId: turn, Status: pb.ItemStatus_ITEM_STATUS_FINISHED,
		Body: &pb.SidecarItem_Assistant{Assistant: &pb.Text{Text: text}},
	}}})
	s.Emit(&pb.SidecarEvent{Body: &pb.SidecarEvent_TurnEnded{TurnEnded: &pb.TurnEnded{
		TurnId: turn, Status: pb.TurnStatus_TURN_STATUS_COMPLETED,
	}}})
}
