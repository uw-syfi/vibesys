package core

import (
	"fmt"
	"time"
)

// SessionSource names the sidecar event an Event came from.
type SessionSource struct {
	Session Session
	Offset  uint64
}

// Event is one fact to commit. All the events of one Decide commit together or not at all.
type Event struct {
	// Run is empty only on the receipt of a command refused before it reached a run.
	Run     RunID
	Command CommandID
	At      time.Time
	// Source is nil unless a sidecar event caused this.
	Source *SessionSource
	Body   EventBody
}

// EventBody is a whole entity, so that a reader replaces by id: Run, Agent, Turn, Item, Message or
// Receipt. The set is closed.
type EventBody interface{ eventBody() }

func (Run) eventBody()     {}
func (Agent) eventBody()   {}
func (Turn) eventBody()    {}
func (Item) eventBody()    {}
func (Message) eventBody() {}
func (Receipt) eventBody() {}

// Effect is one request for I/O. It is carried out at least once, after its events are committed.
// The set is closed.
type Effect interface {
	// ID is stable across redelivery, so whoever carries it out can drop a repeat.
	ID() EffectID
}

// The effects.
type (
	// LaunchSession starts a session, continuing the agent's last one when ResumeToken is set. What
	// else the session needs is the run's, read when the effect is carried out.
	LaunchSession struct {
		Session     Session
		Run         RunID
		Role        AgentRole
		Spec        AgentSpec
		ResumeToken string
	}
	// StartTurn starts a turn on Message.
	StartTurn struct {
		Session Session
		Turn    TurnID
		Message Message
	}
	// Steer adds a message to a running turn.
	Steer struct {
		Session Session
		Turn    TurnID
		Message Message
	}
	// Interrupt asks a running turn to end.
	Interrupt struct {
		Session Session
		Turn    TurnID
	}
	// CloseSession ends a session. Force kills it without waiting: the session has been written
	// off, so nothing listens for its answer.
	CloseSession struct {
		Session Session
		Force   bool
	}
	// ReleaseCheckouts asks for the working copies the agents of a run that is over made to be removed,
	// except those that hold work git does not have. It is answered by CheckoutsReleased.
	ReleaseCheckouts struct{ Run RunID }
	// ArmTimer asks for a TimerFired at FireAt.
	ArmTimer struct {
		Kind TimerKind
		Run  RunID
		// Session is set on a stop grace.
		Session Session
		FireAt  time.Time
	}
)

// ID implements Effect.
func (e LaunchSession) ID() EffectID {
	return EffectID(fmt.Sprintf("launch/%s/%d", e.Session.Agent, e.Session.N))
}

// ID implements Effect.
func (e StartTurn) ID() EffectID { return EffectID("turn/" + string(e.Turn)) }

// ID implements Effect.
func (e Steer) ID() EffectID { return EffectID("steer/" + string(e.Message.ID)) }

// ID implements Effect.
func (e Interrupt) ID() EffectID { return EffectID("interrupt/" + string(e.Turn)) }

// ID implements Effect.
func (e CloseSession) ID() EffectID {
	return EffectID(fmt.Sprintf("close/%s/%d/%t", e.Session.Agent, e.Session.N, e.Force))
}

// ID implements Effect.
func (e ReleaseCheckouts) ID() EffectID { return EffectID("checkouts/" + string(e.Run)) }

// ID implements Effect.
func (e ArmTimer) ID() EffectID { return EffectID("timer/" + string(e.Timer())) }

// Timer is the timer's identity.
func (e ArmTimer) Timer() TimerID {
	switch e.Kind {
	case TimerRunDeadline, TimerLimitGrace:
		return TimerID(fmt.Sprintf("%s/%s", e.Kind, e.Run))
	case TimerLeadIdle, TimerSummaryDue:
		// Each stall, and each time a report comes due, is its own timer: one that ended and began
		// again must not be answered by the timer of the first.
		return TimerID(fmt.Sprintf("%s/%s/%d", e.Kind, e.Run, e.FireAt.UnixNano()))
	}
	return TimerID(fmt.Sprintf("%s/%s/%d", e.Kind, e.Session.Agent, e.Session.N))
}
