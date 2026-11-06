// Package connect reaches a root's host, starting one when nobody serves. Whoever starts a host
// learns how the start went from the host itself, as a HostStartReport, never from a log or from
// watching for a socket.
package connect

import (
	"context"
	"errors"
	"fmt"
	"net"
	"os"
	"syscall"
	"time"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"

	"github.com/uw-syfi/vibesys/gateway/internal/lifecycle"
	"github.com/uw-syfi/vibesys/orchestrator/hostlock"
)

// settleEvery is how often Dial looks again while another process is starting the host.
const settleEvery = 20 * time.Millisecond

// Launcher starts one candidate host and returns what it reported, once the candidate is serving or
// has ended. It fails with *EndedError for a candidate that ended without reporting, and with ctx's
// error when ctx ended first, having ended the candidate.
type Launcher interface {
	Launch(ctx context.Context) (*pb.HostStartReport, error)
}

// RefusedError is a host that said why it will not serve. It has ended, and nothing is left running.
type RefusedError struct{ Why *pb.Unserved }

func (e *RefusedError) Error() string { return e.Why.GetDetail() }

// EndedError is a candidate that ended without saying why: what it wrote on stderr is all there is,
// and it is only what this attempt wrote.
type EndedError struct {
	Status string
	Stderr string
}

func (e *EndedError) Error() string {
	if e.Stderr == "" {
		return "the host ended as it started (" + e.Status + ") and said nothing"
	}
	return "the host ended as it started (" + e.Status + "): " + e.Stderr
}

// Holding says whether some process holds the host's lock now.
type Holding func() (bool, error)

// HeldElsewhereError is a lock held by a process on another machine, seen through a shared disk. Its
// socket cannot be reached from here, however long a start waits.
type HeldElsewhereError struct {
	Root string
	Lock *pb.HostLock
}

func (e *HeldElsewhereError) Error() string {
	return fmt.Sprintf("the host of %s is on %s (process %d, release %s), and this machine can neither reach it nor end it. Run this on %s, or give this machine its own root on a local disk", e.Root, e.Lock.GetMachine(), e.Lock.GetPid(), e.Lock.GetRelease(), e.Lock.GetMachine())
}

// HoldingOf is the Holding of root's lock, which fails with *HeldElsewhereError for a process on
// another machine. A record that does not read cannot say so, and is a holder like any other.
func HoldingOf(root string) Holding {
	return func() (bool, error) {
		record, held, err := lifecycle.Holder(root)
		if err != nil || !held {
			return held, err
		}
		if lock, err := hostlock.Parse(record); err == nil && hostlock.Elsewhere(lock, hostlock.ThisMachine()) {
			return true, &HeldElsewhereError{Root: root, Lock: lock}
		}
		return true, nil
	}
}

// Dial connects to socket, starting a host through launcher when nobody serves it. Concurrent
// starters are sorted out by the host's lock: the loser reports that, and Dial waits for the winner
// and starts again if the winner went away without serving. ctx bounds everything.
func Dial(ctx context.Context, socket string, launcher Launcher, holding Holding) (net.Conn, error) {
	if err := lifecycle.CheckSocketDirectory(socket); err != nil {
		return nil, err
	}
	for {
		connection, err := dial(ctx, socket)
		if err == nil || !Absent(err) {
			return connection, err
		}
		report, err := launcher.Launch(ctx)
		if err != nil {
			return nil, err
		}
		switch outcome := report.GetOutcome().(type) {
		case *pb.HostStartReport_Refused:
			return nil, &RefusedError{Why: outcome.Refused}
		case *pb.HostStartReport_Serving:
			return dial(ctx, socket)
		case *pb.HostStartReport_AlreadyServed:
			connection, err := awaitWinner(ctx, socket, holding)
			if connection != nil || err != nil {
				return connection, err
			}
		default:
			return nil, fmt.Errorf("the host's start report says nothing: %v", report)
		}
	}
}

// awaitWinner waits for the process that holds the lock to serve. It returns nothing at all when the
// lock came free with nobody serving, for the caller to start again.
func awaitWinner(ctx context.Context, socket string, holding Holding) (net.Conn, error) {
	tick := time.NewTicker(settleEvery)
	defer tick.Stop()
	for {
		connection, err := dial(ctx, socket)
		if err == nil || !Absent(err) {
			return connection, err
		}
		held, err := holding()
		if err != nil {
			return nil, err
		}
		if !held {
			return nil, nil
		}
		select {
		case <-ctx.Done():
			return nil, fmt.Errorf("host startup: %w", ctx.Err())
		case <-tick.C:
		}
	}
}

func dial(ctx context.Context, socket string) (net.Conn, error) {
	return (&net.Dialer{}).DialContext(ctx, "unix", socket)
}

// Absent says err is what a dial of a socket that nobody serves returns: the socket is not there,
// or it is a file a host left behind.
func Absent(err error) bool {
	return errors.Is(err, os.ErrNotExist) || errors.Is(err, syscall.ECONNREFUSED)
}
