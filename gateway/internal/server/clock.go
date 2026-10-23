package server

import "time"

// LivenessProbeInterval bounds detection of a writer disappearing without I/O.
const LivenessProbeInterval = 5 * time.Second

// LivenessLease is the protocol's thirty-second observation lease.
const LivenessLease = 30 * time.Second

// Clock supplies lease time and per-subscription tickers. The subscription owns Stop.
type Clock interface {
	Now() time.Time
	NewTicker(time.Duration) Ticker
}

// Ticker coalesces ticks when a subscriber is busy, like time.Ticker.
type Ticker interface {
	Ticks() <-chan time.Time
	Stop()
}

type SystemClock struct{}
type systemTicker struct{ *time.Ticker }

func (SystemClock) Now() time.Time { return time.Now() }

func (SystemClock) NewTicker(interval time.Duration) Ticker {
	return systemTicker{time.NewTicker(interval)}
}
func (t systemTicker) Ticks() <-chan time.Time { return t.C }
