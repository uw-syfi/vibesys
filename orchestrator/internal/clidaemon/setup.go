package clidaemon

import (
	"context"
	"errors"
	"fmt"

	"buf.build/go/protovalidate"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	cli "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
)

// SetupDescriber is the call of the CLI daemon that reads a draft task's setup facts.
type SetupDescriber interface {
	DescribeSetup(context.Context, *cli.DescribeSetupRequest) (*cli.DescribeSetupResponse, error)
}

// SetupConnection is a SetupDescriber the caller closes.
type SetupConnection interface {
	SetupDescriber
	Close() error
}

// SetupConnector dials the socket the tool answered.
type SetupConnector func(context.Context, Socket) (SetupConnection, error)

// DescribeSetup calls the daemon. The caller's context must carry a deadline: a read is bounded by it.
func (c *Client) DescribeSetup(ctx context.Context, r *cli.DescribeSetupRequest) (*cli.DescribeSetupResponse, error) {
	if _, ok := ctx.Deadline(); !ok {
		return nil, errors.New("DescribeSetup requires a caller deadline")
	}
	return c.svc.DescribeSetup(ctx, r)
}

// SetupAnswer is what the daemon said of a draft task: its facts, or why the task cannot be read, in
// the reader's words. Exactly one is set.
type SetupAnswer struct {
	Facts      *cli.SetupFacts
	Unreadable string
}

// SetupReader reads a draft task's setup facts from a host's CLI daemon: it asks the tool where the
// daemon is, connects, asks, and closes.
type SetupReader struct {
	Ensurer Ensurer
	Connect SetupConnector
}

// Read returns the daemon's answer, held to the contract. A daemon that does not know the call is an
// error that says so, and never an answer: setup facts that were not read pass nothing.
func (s SetupReader) Read(ctx context.Context, repository, task string) (SetupAnswer, error) {
	socket, err := s.Ensurer.Ensure(ctx)
	if err != nil {
		return SetupAnswer{}, err
	}
	conn, err := s.Connect(ctx, socket)
	if err != nil {
		return SetupAnswer{}, err
	}
	defer conn.Close()
	return DescribeSetup(ctx, conn, repository, task)
}

// DescribeSetup asks a daemon for the setup facts of a task in a repository.
func DescribeSetup(ctx context.Context, svc SetupDescriber, repository, task string) (SetupAnswer, error) {
	resp, err := svc.DescribeSetup(ctx, &cli.DescribeSetupRequest{Repository: repository, Task: task})
	if status.Code(err) == codes.Unimplemented {
		return SetupAnswer{}, errors.New("the CLI daemon cannot describe a task's setup: it is older than this host")
	}
	if err != nil {
		return SetupAnswer{}, err
	}
	if err := protovalidate.Validate(resp); err != nil {
		return SetupAnswer{}, fmt.Errorf("the CLI daemon's setup facts are not valid: %w", err)
	}
	if u := resp.GetUnreadable(); u != nil {
		return SetupAnswer{Unreadable: u.GetDetail()}, nil
	}
	return SetupAnswer{Facts: resp.GetFacts()}, nil
}
