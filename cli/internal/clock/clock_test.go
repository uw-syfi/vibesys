package clock_test

import (
	"sync/atomic"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/clock"
)

func TestGracefulLeavesAGentleStopThatEnds(t *testing.T) {
	var forced atomic.Bool
	clock.Graceful(clock.NewManualTimers(), time.Hour, func() {}, func() { forced.Store(true) })
	if forced.Load() {
		t.Fatal("a stop that ended was forced")
	}
}

func TestGracefulForcesAGentleStopThatDoesNotEnd(t *testing.T) {
	timers := clock.NewManualTimers()
	unblock := make(chan struct{})
	done := make(chan struct{})
	go func() {
		defer close(done)
		clock.Graceful(timers, time.Hour, func() { <-unblock }, func() { close(unblock) })
	}()
	<-timers.Armed()
	timers.Fire()
	<-done
}
