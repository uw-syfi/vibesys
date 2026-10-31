package server

import (
	"time"

	"github.com/uw-syfi/vibesys/gateway/internal/clock"
)

// LivenessProbeInterval bounds detection of a writer disappearing without I/O.
const LivenessProbeInterval = 5 * time.Second

// LivenessLease is the protocol's thirty-second observation lease.
const LivenessLease = 30 * time.Second

// Clock is the gateway's injected time, shared with its subscription hub.
type Clock = clock.Clock

// Ticker coalesces ticks when a subscriber is busy, like time.Ticker.
type Ticker = clock.Ticker

// SystemClock is the real clock.
type SystemClock = clock.System
