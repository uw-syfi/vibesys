package setup

import "time"

// The cadence of reads of one run's setup facts.
const (
	// MinGap is the least time between the starts of two reads.
	MinGap = time.Second
	// Idle is the wait after a read that found something new; it doubles for each read in a row that
	// found nothing, up to IdleMax.
	Idle    = 5 * time.Second
	IdleMax = 30 * time.Second
	// FailureMin is the wait after a failed read; it doubles for each failure in a row, up to
	// FailureMax.
	FailureMin = 2 * time.Second
	FailureMax = 60 * time.Second
)

// Outcome is how a finished read ended.
type Outcome uint8

// The outcomes. ReadNone means no read finished.
const (
	ReadNone Outcome = iota
	ReadChanged
	ReadUnchanged
	ReadFailed
)

// Events is what happened since the last call of Next.
type Events struct {
	// Triggered is true when something that may have changed the facts happened: a run event or a
	// search revision. Any number of them are the same as one.
	Triggered bool
	// Finished is the outcome of the read that was in flight, if it ended.
	Finished Outcome
}

// Action is what to do now: start a read, or wait until Until (zero while a read is in flight, which
// ends the wait by itself).
type Action struct {
	Read  bool
	Until time.Time
}

// Schedule is the state of one run's cadence. The zero value has never read.
type Schedule struct {
	started   bool
	inFlight  bool
	dirty     bool
	lastStart time.Time
	due       time.Time
	unchanged int
	failures  int
}

// Next advances the schedule to now given what happened, and says what to do. It reads no clock:
// the time is the caller's. At most one read is in flight; reads start at least MinGap apart; any
// number of triggers during a read or inside the gap make exactly one more read; with no trigger a
// read follows after Idle, backed off while reads find nothing, or after the failure wait.
func Next(s Schedule, now time.Time, ev Events) (Schedule, Action) {
	if s.inFlight && ev.Finished != ReadNone {
		s.inFlight = false
		switch ev.Finished {
		case ReadChanged:
			s.unchanged, s.failures = 0, 0
			s.due = now.Add(Idle)
		case ReadUnchanged:
			s.unchanged++
			s.failures = 0
			s.due = now.Add(backoff(Idle, IdleMax, s.unchanged-1))
		case ReadFailed:
			s.failures++
			s.due = now.Add(backoff(FailureMin, FailureMax, s.failures-1))
		}
	}
	if ev.Triggered {
		s.dirty = true
		s.unchanged = 0
	}
	if s.inFlight {
		return s, Action{}
	}
	if !s.started {
		return start(s, now), Action{Read: true}
	}
	target := s.lastStart.Add(MinGap)
	if !s.dirty || s.failures > 0 {
		target = laterOf(target, s.due)
	}
	if now.Before(target) {
		return s, Action{Until: target}
	}
	return start(s, now), Action{Read: true}
}

func start(s Schedule, now time.Time) Schedule {
	s.started, s.inFlight, s.dirty, s.lastStart = true, true, false, now
	return s
}

func laterOf(a, b time.Time) time.Time {
	if b.After(a) {
		return b
	}
	return a
}

// backoff is base doubled n times, capped at limit.
func backoff(base, limit time.Duration, n int) time.Duration {
	d := base
	for range max(n, 0) {
		d *= 2
		if d >= limit {
			return limit
		}
	}
	return min(d, limit)
}
