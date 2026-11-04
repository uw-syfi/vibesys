package clock_test

import (
	"context"
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

// Exactly fires only the timeout it names, and hands every other to the timers behind it.
func TestExactlyFiresOnlyItsOwnTimeout(t *testing.T) {
	others := clock.NewManualTimers()
	e := clock.Exactly{D: 45 * time.Minute, Others: others}
	fired, stop := e.Timeout(context.Background(), 45*time.Minute)
	defer stop()
	select {
	case <-fired:
	default:
		t.Fatal("the named timeout did not fire at once")
	}
	other, stopOther := e.Timeout(context.Background(), 30*time.Minute)
	defer stopOther()
	if d := <-others.Armed(); d != 30*time.Minute {
		t.Fatalf("the other timeout reached the timers behind as %v", d)
	}
	select {
	case <-other:
		t.Fatal("another timeout fired")
	default:
	}
}
