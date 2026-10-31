// Package clock is the gateway's injected time: lease reads and tickers that
// conflate when their reader is busy.
package clock

import "time"

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

// System is the real clock.
type System struct{}

type systemTicker struct{ *time.Ticker }

func (System) Now() time.Time { return time.Now() }

func (System) NewTicker(interval time.Duration) Ticker {
	return systemTicker{time.NewTicker(interval)}
}

func (t systemTicker) Ticks() <-chan time.Time { return t.C }
