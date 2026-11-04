package core

import (
	"fmt"
	"time"
)

// WaitKind is why a queued job has not started. The set is closed.
type WaitKind string

// The kinds of wait.
const (
	// WaitFull is a resource with no room left for the job's claim: its holders use all of it.
	WaitFull WaitKind = "full"
	// WaitHeldExclusively is a resource a holder claims for itself alone.
	WaitHeldExclusively WaitKind = "held exclusively"
	// WaitInUse is a resource the job claims for itself alone, which holders use.
	WaitInUse WaitKind = "in use"
	// WaitBehind is a resource an earlier job waits for, which keeps its turn: a job that came later
	// does not take what that one needs.
	WaitBehind WaitKind = "behind"
	// WaitHeld is a rule of the scheduler that holds the job back, which the detail names.
	WaitHeld WaitKind = "held"
	// WaitTrouble is a job or a store whose trouble the job waits for to pass, which the detail
	// names.
	WaitTrouble WaitKind = "trouble"
	// WaitLeased is a resource that a daemon other than this one, or another of this daemon's
	// jobs, holds the lease of; Elsewhere names who.
	WaitLeased WaitKind = "leased"
)

// WaitKinds lists every kind of wait.
func WaitKinds() []WaitKind {
	return []WaitKind{WaitFull, WaitHeldExclusively, WaitInUse, WaitBehind, WaitHeld, WaitTrouble, WaitLeased}
}

// JobWait says why a queued job has not started, as the daemon's scheduler decided it at its last
// change. It is derived from what the daemon holds and is never stored.
type JobWait struct {
	Kind WaitKind
	// Resource is the resource the job waits for, and empty for WaitHeld and WaitTrouble.
	Resource string
	// Holders are the jobs that hold the resource, or for WaitBehind the earlier jobs that wait for
	// it, in the order the daemon heard of them.
	Holders []WaitHolder
	// Detail is the rule or the trouble for WaitHeld and WaitTrouble, in words.
	Detail string
	// Elsewhere is who holds the lease for WaitLeased, and nil otherwise.
	Elsewhere *LeaseHolder
}

// LeaseHolder is who holds the lease of a resource: the user and the machine a daemon runs as and
// on, its process, the job it holds the lease for, and since when.
type LeaseHolder struct {
	User  string
	Host  string
	PID   int
	Job   JobID
	Since time.Time
}

// String names the holder in words, such as "alice on host-a (pid 41), job 7f3…".
func (h LeaseHolder) String() string {
	who := h.User
	if who == "" {
		who = "someone"
	}
	if h.Host != "" {
		who += " on " + h.Host
	}
	if h.PID > 0 {
		who += fmt.Sprintf(" (pid %d)", h.PID)
	}
	if h.Job != "" {
		who += ", job " + string(h.Job)
	}
	return who
}

// WaitHolder is a job that stands in another's way. Task is the task whose store it is in, empty
// when that is not known, and Run the run that submitted it first, empty when it named none.
type WaitHolder struct {
	Job  JobID
	Task TaskName
	Kind JobKind
	Run  RunID
}
