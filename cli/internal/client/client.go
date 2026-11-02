package client

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/runtimedir"
	"github.com/uw-syfi/vibesys/cli/internal/wire"
)

// Starter starts a daemon and returns once it serves, or says why it does not. It fails with
// runtimedir.ErrDaemonRunning when another daemon holds the machine.
type Starter interface {
	Start(ctx context.Context) error
}

// Rendezvous is how the clients of a machine meet the one daemon: the connection to it, and the two
// waits around starting it. LocalRendezvous is the shipped one, over the runtime directory.
type Rendezvous interface {
	// Dial connects to the daemon's socket.
	Dial(ctx context.Context) (net.Conn, error)
	// HoldStart waits for the right to start a daemon, which one client at a time has, and returns
	// what gives it up.
	HoldStart(ctx context.Context) (release func() error, err error)
	// AwaitExit returns once no daemon holds the machine. It does not poll. It can wait as long as
	// a daemon lives, so a caller bounds it.
	AwaitExit(ctx context.Context) error
}

// LocalRendezvous is the Rendezvous of the opened runtime directory.
type LocalRendezvous struct{ Dir runtimedir.Dir }

var _ Rendezvous = LocalRendezvous{}

// Dial implements Rendezvous.
func (l LocalRendezvous) Dial(ctx context.Context) (net.Conn, error) {
	return (&net.Dialer{}).DialContext(ctx, "unix", l.Dir.Socket())
}

// HoldStart implements Rendezvous.
func (l LocalRendezvous) HoldStart(ctx context.Context) (func() error, error) {
	lock, err := runtimedir.WaitLock(ctx, l.Dir.StartLock())
	if err != nil {
		return nil, err
	}
	return lock.Unlock, nil
}

// AwaitExit implements Rendezvous. The instance lock is held for as long as a daemon lives, so
// taking it is seeing the daemon gone.
func (l LocalRendezvous) AwaitExit(ctx context.Context) error {
	lock, err := runtimedir.WaitLock(ctx, l.Dir.DaemonLock())
	if err != nil {
		return err
	}
	return lock.Unlock()
}

// Options is what a client needs to reach a daemon.
type Options struct {
	// Rendezvous is how to reach the daemon.
	Rendezvous Rendezvous
	// Version is this build's release version, sent in the handshake.
	Version string
	// Starter starts a daemon when none runs. Nil means never start one.
	Starter Starter
	// Timers makes the timeouts of a client. It defaults to the system's.
	Timers clock.Timers
}

func (o Options) withDefaults() Options {
	if o.Timers == nil {
		o.Timers = clock.System{}
	}
	return o
}

const (
	// stopBound is how long a daemon that was told to stop gets to go.
	stopBound = time.Minute
	// settleBound is how long a client waits for a daemon that holds the machine and does not
	// answer yet, as it is going or still recovering, before it looks again.
	settleBound = 5 * time.Second
	// startRounds is how many times a client looks for a daemon and tries to start one.
	startRounds = 8
)

// ErrNoDaemon says no daemon answers on the socket.
var ErrNoDaemon = wire.Unavailable("no daemon is running")

// Client is a connection to the daemon that passed the version handshake. Close it when done.
type Client struct {
	opts Options
	conn *grpc.ClientConn
	rpc  cliv1.DaemonServiceClient
}

// Dial connects to a running daemon and does not start one. It fails with ErrNoDaemon when none
// answers, and with a failure of kind incompatible when the builds cannot talk.
func Dial(ctx context.Context, opts Options) (*Client, error) {
	c, answer, err := dial(ctx, opts)
	if err != nil {
		return nil, err
	}
	if err := wire.CompatibilityError(answer); err != nil {
		c.Close()
		return nil, err
	}
	return c, nil
}

// Connect connects to the daemon and starts one when none runs. Clients that find none at once
// start a single daemon between them. A daemon older than this client is stopped and replaced;
// the jobs it was running keep running under their supervisors. A client older than the daemon
// is refused, since it is the one to upgrade.
func Connect(ctx context.Context, opts Options) (*Client, error) {
	opts = opts.withDefaults()
	c, answer, err := dial(ctx, opts)
	switch {
	case err == nil && answer == cliv1.Compatibility_COMPATIBILITY_COMPATIBLE:
		return c, nil
	case err == nil && wire.DaemonTooOld(answer) && opts.Starter != nil:
		c.Close()
	case err == nil:
		c.Close()
		return nil, wire.CompatibilityError(answer)
	case !errors.Is(err, ErrNoDaemon):
		return nil, err
	case opts.Starter == nil:
		return nil, ErrNoDaemon
	}
	return start(ctx, opts)
}

// start brings up the daemon that clients of this build can use. It holds the right to start for
// all of it, which is why two clients upgrading a daemon at once do not stop each other's new one:
// the second looks after the first is done, and finds the new daemon. Every wait in it is bounded,
// and the rounds are too, so a daemon that neither answers nor goes ends in an error.
func start(ctx context.Context, opts Options) (*Client, error) {
	release, err := opts.Rendezvous.HoldStart(ctx)
	if err != nil {
		return nil, err
	}
	defer release()

	for range startRounds {
		c, answer, err := dial(ctx, opts)
		switch {
		case err == nil && answer == cliv1.Compatibility_COMPATIBILITY_COMPATIBLE:
			return c, nil
		case err == nil && wire.DaemonTooOld(answer):
			err := c.stopAndWait(ctx)
			c.Close()
			if err != nil {
				return nil, err
			}
		case err == nil:
			c.Close()
			return nil, wire.CompatibilityError(answer)
		case !errors.Is(err, ErrNoDaemon):
			return nil, err
		}
		err = opts.Starter.Start(ctx)
		switch {
		case err == nil:
			return Dial(ctx, opts)
		case !errors.Is(err, runtimedir.ErrDaemonRunning):
			return nil, err
		}
		// A daemon holds the machine and did not answer: it is going, or it has not come up.
		if err := awaitExit(ctx, opts, settleBound); err != nil {
			return nil, err
		}
	}
	return nil, wire.Unavailable("a daemon holds this machine and does not answer; see `daemon status` and the daemon log")
}

// awaitExit waits for the daemon to go, for at most bound. A bound that passes is not a failure: a
// caller looks at what it finds next.
func awaitExit(ctx context.Context, opts Options, bound time.Duration) error {
	bounded, done := clock.Deadline(ctx, opts.Timers, bound)
	defer done()
	err := opts.Rendezvous.AwaitExit(bounded)
	if err != nil && ctx.Err() != nil {
		return ctx.Err()
	}
	return nil
}

// Restart stops the daemon if one runs, waits until it is gone, and starts a new one. Jobs that
// run are not affected: the new daemon finds their supervisors.
func Restart(ctx context.Context, opts Options) (*Client, error) {
	opts = opts.withDefaults()
	if opts.Starter == nil {
		return nil, errors.New("restarting the daemon needs a way to start one")
	}
	release, err := opts.Rendezvous.HoldStart(ctx)
	if err != nil {
		return nil, err
	}
	defer release()

	c, _, err := dial(ctx, opts)
	switch {
	case err == nil:
		defer c.Close()
		if err := c.stopAndWait(ctx); err != nil {
			return nil, err
		}
	case !errors.Is(err, ErrNoDaemon):
		return nil, err
	}
	if err := opts.Starter.Start(ctx); err != nil {
		return nil, err
	}
	return Dial(ctx, opts)
}

// dial connects and says Hello. It fails with ErrNoDaemon when the socket does not answer.
func dial(ctx context.Context, opts Options) (*Client, cliv1.Compatibility, error) {
	opts = opts.withDefaults()
	conn, err := grpc.NewClient("passthrough:///daemon",
		grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) {
			return opts.Rendezvous.Dial(ctx)
		}),
		grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		return nil, 0, err
	}
	c := &Client{opts: opts, conn: conn, rpc: cliv1.NewDaemonServiceClient(conn)}
	resp, err := c.rpc.Hello(ctx, &cliv1.HelloRequest{
		Component: cliv1.Component_COMPONENT_CLI,
		Versions:  wire.CurrentVersions(opts.Version),
	})
	if err != nil {
		conn.Close()
		if wire.IsUnavailable(err) {
			return nil, 0, ErrNoDaemon
		}
		return nil, 0, c.fail(err)
	}
	return c, resp.GetCompatibility(), nil
}

// stopAndWait stops the daemon this client is connected to and returns once it has gone.
func (c *Client) stopAndWait(ctx context.Context) error {
	if _, err := c.rpc.Stop(ctx, &cliv1.StopRequest{}); err != nil && !wire.IsUnavailable(err) {
		// A daemon that goes before it answers is what was asked for; the lock below says when.
		return c.fail(err)
	}
	bounded, done := clock.Deadline(ctx, c.opts.Timers, stopBound)
	defer done()
	if err := c.opts.Rendezvous.AwaitExit(bounded); err != nil {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		return wire.Unavailable(fmt.Sprintf("the daemon was told to stop and did not go within %s", stopBound))
	}
	return nil
}

// Close releases the connection.
func (c *Client) Close() error { return c.conn.Close() }

// fail returns the error of a call as the failure it carries. A call the caller cancelled keeps
// its own error.
func (c *Client) fail(err error) error {
	if err == nil {
		return nil
	}
	if f, ok := wire.FailureOf(err); ok {
		return &f
	}
	return err
}

// Status returns the daemon's status.
func (c *Client) Status(ctx context.Context) (*cliv1.StatusResponse, error) {
	resp, err := c.rpc.Status(ctx, &cliv1.StatusRequest{})
	return resp, c.fail(err)
}

// Stop asks the daemon to end and returns once it has gone. Jobs that run are not affected.
func (c *Client) Stop(ctx context.Context) error { return c.stopAndWait(ctx) }

// Submit queues a job.
func (c *Client) Submit(ctx context.Context, req *cliv1.SubmitRequest) (*cliv1.SubmitResponse, error) {
	resp, err := c.rpc.Submit(ctx, req)
	return resp, c.fail(err)
}

// Cancel cancels a job and returns it once it is final.
func (c *Client) Cancel(ctx context.Context, req *cliv1.CancelRequest) (*cliv1.CancelResponse, error) {
	resp, err := c.rpc.Cancel(ctx, req)
	return resp, c.fail(err)
}

// AddNote adds a note to a job.
func (c *Client) AddNote(ctx context.Context, req *cliv1.AddNoteRequest) (*cliv1.AddNoteResponse, error) {
	resp, err := c.rpc.AddNote(ctx, req)
	return resp, c.fail(err)
}

// ListJobs lists the jobs of a task.
func (c *Client) ListJobs(ctx context.Context, req *cliv1.ListJobsRequest) (*cliv1.ListJobsResponse, error) {
	resp, err := c.rpc.ListJobs(ctx, req)
	return resp, c.fail(err)
}

// Logs passes a job's output to emit as it is read, until the end of it, or with follow until the
// job is final.
func (c *Client) Logs(ctx context.Context, req *cliv1.ReadLogsRequest, emit func(*cliv1.ReadLogsResponse) error) error {
	stream, err := c.rpc.ReadLogs(ctx, req)
	if err != nil {
		return c.fail(err)
	}
	for {
		resp, err := stream.Recv()
		if err != nil {
			return streamEnd(c, err)
		}
		if err := emit(resp); err != nil {
			return err
		}
	}
}

// streamEnd is the error of a finished stream: nothing when it ended well.
func streamEnd(c *Client, err error) error {
	if errors.Is(err, io.EOF) {
		return nil
	}
	return c.fail(err)
}
