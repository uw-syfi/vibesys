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

// Reaction is what a session does when it is sent a command. The default one is DefaultReaction.
type Reaction func(s *Session, cmd *pb.SessionCommand)

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
	// Opening, when set, is what a launched session says as it starts, in place of opening at once.
	// It returns the session's first events: a failure to start, or none, which leaves the session
	// launching until something else ends it.
	Opening func(l core.LaunchSession) []*pb.SessionEvent
	// LaunchError, when set, is what a failing launch reports.
	LaunchError error
	// KillFailures makes that many Kill calls fail before any succeeds, as a signal that cannot be
	// delivered does.
	KillFailures int
	// SendError, when set, is what every Send to a session returns, as a sidecar whose socket
	// vanished does before the daemon has seen its stream end.
	SendError error
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
func (h *Sidecars) Launch(ctx context.Context, start daemon.SessionStart) (daemon.Sidecar, error) {
	l := start.Launch
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
	s := &Session{Launch: l, Start: start, Credentials: start.Tools, seen: map[string]bool{}, host: h}
	h.sessions[l.Session] = s
	if h.Opening != nil {
		for _, ev := range h.Opening(l) {
			s.Emit(ev)
		}
		return &conn{host: h, s: s}, nil
	}
	token := l.ResumeToken
	if token == "" {
		token = fmt.Sprintf("conv-%s", l.Session.Agent)
	}
	s.Emit(&pb.SessionEvent{Body: &pb.SessionEvent_Opened{Opened: &pb.SessionOpened{
		ResumeToken:  []byte(token),
		Capabilities: &pb.Capabilities{Steers: true, Resumes: true},
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
	Start  daemon.SessionStart
	// Credentials is what the session's tool server was told, which holds its token.
	Credentials *pb.ToolServerCredentials
	// Received lists each command once, by effect id.
	Received []*pb.SessionCommand

	mu     sync.Mutex
	events []*pb.SessionEvent
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
func (s *Session) Emit(ev *pb.SessionEvent) {
	if ev.Offset == 0 {
		ev.Offset = uint64(len(s.events)) + 1
	}
	s.events = append(s.events, ev)
}

// EmitEvents appends events under the session's lock.
func (s *Session) EmitEvents(evs ...*pb.SessionEvent) {
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
func (s *Session) Commands() []*pb.SessionCommand {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]*pb.SessionCommand(nil), s.Received...)
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

func (c *conn) Send(ctx context.Context, cmd *pb.SessionCommand) error {
	if hung, err := c.host.hang(ctx); hung {
		return err
	}
	if c.host.SendError != nil {
		return c.host.SendError
	}
	err := c.send(cmd)
	if c.host.AfterSend != nil {
		c.host.AfterSend()
	}
	return err
}

func (c *conn) send(cmd *pb.SessionCommand) error {
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

func (st *stream) Next() (*pb.SessionEvent, error) {
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
func DefaultReaction(s *Session, cmd *pb.SessionCommand) {
	switch b := cmd.GetBody().(type) {
	case *pb.SessionCommand_StartTurn:
		AnswerTurn(s, b.StartTurn.GetTurnId(), "ok")
	case *pb.SessionCommand_Steer:
		s.Emit(&pb.SessionEvent{Body: &pb.SessionEvent_Steer{Steer: &pb.SteerOutcome{
			MessageId: b.Steer.GetMessageId(), Accepted: true,
		}}})
	case *pb.SessionCommand_Interrupt:
		s.Emit(&pb.SessionEvent{Body: &pb.SessionEvent_TurnEnded{TurnEnded: &pb.TurnEnded{
			TurnId: b.Interrupt.GetTurnId(), Status: pb.TurnStatus_TURN_STATUS_INTERRUPTED,
		}}})
	case *pb.SessionCommand_Close:
		s.closed = true
		s.Emit(&pb.SessionEvent{Body: &pb.SessionEvent_Closed{Closed: &pb.SessionClosed{}}})
	}
}

// AnswerTurn emits what a turn that writes `text` and ends looks like: a delta, the finished item,
// and the end of the turn. The session's lock must be held.
func AnswerTurn(s *Session, turn, text string) { AnswerTurnWith(s, turn, text, nil) }

// AnswerTurnWith is AnswerTurn for a turn whose end reports usage, nil for none.
func AnswerTurnWith(s *Session, turn, text string, usage *pb.Usage) {
	item := "item-" + turn
	s.Emit(&pb.SessionEvent{Body: &pb.SessionEvent_Delta{Delta: &pb.SessionDelta{
		ItemId: item, Body: &pb.SessionDelta_Text{Text: text},
	}}})
	s.Emit(&pb.SessionEvent{Body: &pb.SessionEvent_Item{Item: &pb.SessionItem{
		Id: item, TurnId: turn, Status: pb.ItemStatus_ITEM_STATUS_FINISHED,
		Body: &pb.SessionItem_Assistant{Assistant: &pb.Text{Text: text}},
	}}})
	s.Emit(&pb.SessionEvent{Body: &pb.SessionEvent_TurnEnded{TurnEnded: &pb.TurnEnded{
		TurnId: turn, Status: pb.TurnStatus_TURN_STATUS_COMPLETED, Usage: usage,
	}}})
}
