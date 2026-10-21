package server

import "time"

// LivenessProbeInterval bounds detection of a writer disappearing without I/O.
const LivenessProbeInterval = 5 * time.Second

// Clock creates per-subscription tickers. The subscription owns Stop.
type Clock interface {
	NewTicker(time.Duration) Ticker
}

// Ticker coalesces ticks when a subscriber is busy, like time.Ticker.
type Ticker interface {
	Ticks() <-chan time.Time
	Stop()
}

type SystemClock struct{}
type systemTicker struct{ *time.Ticker }

func (SystemClock) NewTicker(interval time.Duration) Ticker {
	return systemTicker{time.NewTicker(interval)}
}
func (t systemTicker) Ticks() <-chan time.Time { return t.C }
