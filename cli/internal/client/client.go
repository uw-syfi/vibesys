package client

import (
	"context"
	"errors"
	"io"
	"net"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/runtimedir"
	"github.com/uw-syfi/vibesys/cli/internal/wire"
)

// Starter starts a daemon and returns once it serves, or says why it does not.
type Starter interface {
	Start(ctx context.Context) error
}

// Options is what a client needs to reach a daemon.
type Options struct {
	// Dir is the opened runtime directory.
	Dir runtimedir.Dir
	// Version is this build's release version, sent in the handshake.
	Version string
	// Starter starts a daemon when none runs. Nil means never start one.
	Starter Starter
	// Timers makes the timeout of a wait. It defaults to the system's.
	Timers clock.Timers
}

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
	c, answer, err := dial(ctx, opts)
	switch {
	case err == nil && answer == cliv1.Compatibility_COMPATIBILITY_COMPATIBLE:
		return c, nil
	case err == nil && wire.DaemonTooOld(answer):
		defer c.Close()
		if opts.Starter == nil {
			return nil, wire.CompatibilityError(answer)
		}
		if err := c.stopAndWait(ctx); err != nil {
			return nil, err
		}
	case err == nil:
		c.Close()
		return nil, wire.CompatibilityError(answer)
	case !errors.Is(err, ErrNoDaemon):
		return nil, err
	}
	if opts.Starter == nil {
		return nil, ErrNoDaemon
	}
	return start(ctx, opts)
}

// start starts a daemon under the start lock. A client that waited for the lock looks again
// before it starts one: the client that held it may have started the daemon already.
func start(ctx context.Context, opts Options) (*Client, error) {
	lock, err := runtimedir.WaitLock(ctx, opts.Dir.StartLock())
	if err != nil {
		return nil, err
	}
	defer lock.Unlock()

	c, answer, err := dial(ctx, opts)
	switch {
	case err == nil:
		if err := wire.CompatibilityError(answer); err != nil {
			c.Close()
			return nil, err
		}
		return c, nil
	case !errors.Is(err, ErrNoDaemon):
		return nil, err
	}
	if err := opts.Starter.Start(ctx); err != nil {
		return nil, err
	}
	return Dial(ctx, opts)
}

// Restart stops the daemon if one runs, waits until it is gone, and starts a new one. Jobs that
// run are not affected: the new daemon finds their supervisors.
func Restart(ctx context.Context, opts Options) (*Client, error) {
	if opts.Starter == nil {
		return nil, errors.New("restarting the daemon needs a way to start one")
	}
	lock, err := runtimedir.WaitLock(ctx, opts.Dir.StartLock())
	if err != nil {
		return nil, err
	}
	defer lock.Unlock()

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
	conn, err := grpc.NewClient("passthrough:///daemon",
		grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) {
			return (&net.Dialer{}).DialContext(ctx, "unix", opts.Dir.Socket())
		}),
		grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		return nil, 0, err
	}
	if opts.Timers == nil {
		opts.Timers = clock.System{}
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

// stopAndWait stops the daemon this client is connected to and returns once it has gone. Its
// instance lock is held for as long as it lives, so waiting for that lock is waiting for it to
// exit, without polling.
func (c *Client) stopAndWait(ctx context.Context) error {
	if _, err := c.rpc.Stop(ctx, &cliv1.StopRequest{}); err != nil && !wire.IsUnavailable(err) {
		// A daemon that goes before it answers is what was asked for; the lock below says when.
		return c.fail(err)
	}
	lock, err := runtimedir.WaitLock(ctx, c.opts.Dir.DaemonLock())
	if err != nil {
		return err
	}
	return lock.Unlock()
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
