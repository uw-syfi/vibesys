package rpc

import (
	"context"
	"errors"
	"fmt"
	"io"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/lifecycle"
	"github.com/uw-syfi/vibesys/cli/internal/supervisor"
	"github.com/uw-syfi/vibesys/cli/internal/wire"
)

// Client is a supervisor.Handle that calls a SupervisorService.
type Client struct {
	rpc cliv1.SupervisorServiceClient
}

var _ supervisor.Handle = (*Client)(nil)

// Connect makes a handle on the connection and runs the version handshake, which must find the
// two builds compatible. A supervisor that does not answer is supervisor.ErrGone.
func Connect(ctx context.Context, conn grpc.ClientConnInterface, binaryVersion string) (*Client, error) {
	c := &Client{rpc: cliv1.NewSupervisorServiceClient(conn)}
	res, err := c.rpc.Handshake(ctx, &cliv1.HandshakeRequest{
		Component: cliv1.Component_COMPONENT_DAEMON,
		Versions:  wire.BuildVersions(binaryVersion, core.CurrentVersions()),
	})
	if err != nil {
		return nil, mapError(ctx, err)
	}
	if got := wire.ToCompatibility(res.GetCompatibility()); got != core.Compatible {
		return nil, &core.Failure{Kind: core.FailureIncompatible, Message: fmt.Sprintf(
			"the supervisor (build %s) cannot talk to this daemon: %s", res.GetVersions().GetBinaryVersion(), got)}
	}
	return c, nil
}

// mapError turns an error of a call made with ctx into the one the Handle contract names.
func mapError(ctx context.Context, err error) error {
	if err == nil {
		return nil
	}
	if ctx.Err() != nil {
		// The caller's own cancel or deadline, whatever shape the transport gave it.
		return ctx.Err()
	}
	st, ok := status.FromError(err)
	if !ok {
		return err
	}
	for _, d := range st.Details() {
		if f, isFailure := d.(*cliv1.Failure); isFailure {
			failure := wire.ToFailure(f)
			return &failure
		}
	}
	switch st.Code() {
	case codes.Unavailable, codes.Canceled:
		// A call cancelled while the caller's context is live was cancelled by a connection that
		// closed under it, which is a supervisor that is gone.
		return supervisor.ErrGone
	}
	return &core.Failure{Kind: core.FailureInternal, Message: st.Message()}
}

// Start implements supervisor.Handle.
func (c *Client) Start(ctx context.Context) error {
	_, err := c.rpc.Start(ctx, &cliv1.StartRequest{})
	return mapError(ctx, err)
}

// Cancel implements supervisor.Handle.
func (c *Client) Cancel(ctx context.Context, reason string) error {
	_, err := c.rpc.CancelJob(ctx, &cliv1.CancelJobRequest{Reason: reason})
	return mapError(ctx, err)
}

// Release implements supervisor.Handle.
func (c *Client) Release(ctx context.Context) error {
	_, err := c.rpc.Release(ctx, &cliv1.ReleaseRequest{})
	return mapError(ctx, err)
}

// Follow implements supervisor.Handle.
func (c *Client) Follow(ctx context.Context, offset uint64, emit func([]byte) error) (lifecycle.Outcome, error) {
	// The stream lives only as long as this call: leaving early ends it on the server too.
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	stream, err := c.rpc.Attach(ctx, &cliv1.AttachRequest{Offset: offset})
	if err != nil {
		return lifecycle.Outcome{}, mapError(ctx, err)
	}
	pos := offset
	for {
		msg, err := stream.Recv()
		if errors.Is(err, io.EOF) {
			// The stream ended without an outcome: the supervisor went away mid-way.
			return lifecycle.Outcome{}, supervisor.ErrGone
		}
		if err != nil {
			if ctx.Err() != nil {
				return lifecycle.Outcome{}, ctx.Err()
			}
			return lifecycle.Outcome{}, mapError(ctx, err)
		}
		switch item := msg.GetItem().(type) {
		case *cliv1.AttachResponse_Output:
			chunk := item.Output
			if chunk.GetOffset() != pos {
				return lifecycle.Outcome{}, &core.Failure{Kind: core.FailureInternal, Message: fmt.Sprintf(
					"the supervisor sent output at offset %d where %d was due", chunk.GetOffset(), pos)}
			}
			if err := emit(chunk.GetData()); err != nil {
				return lifecycle.Outcome{}, err
			}
			pos += uint64(len(chunk.GetData()))
		case *cliv1.AttachResponse_Outcome:
			return wire.ToOutcome(item.Outcome), nil
		default:
			return lifecycle.Outcome{}, &core.Failure{Kind: core.FailureInternal, Message: "the supervisor sent an empty message"}
		}
	}
}
