package clidaemon

import (
	"context"
	"errors"
	pb "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

// Connection is owned by a follower iteration and closed on every exit path.
type Connection interface {
	Service
	Close() error
}

// Connector dials the socket the tool answered. It returns a typed core.NoDaemon when
// dialing fails, without starting anything.
type Connector func(context.Context, Socket) (Connection, error)

// Bound derives a unary deadline from the caller, using an injected clock.
type Bound func(context.Context) (context.Context, context.CancelFunc)

// Follower reconnects streams, keeping the last committed Session across cuts.
// Changed receives owned snapshots. Failed marks the view unreadable without
// clearing it. The socket comes from the tool: the first connection asks with Ensure, and it is kept
// across cuts, so a daemon that keeps answering is read whatever else has changed. One failed
// connection may run Ensure once, before reconnecting.
type Follower struct {
	Connect Connector
	Ensurer Ensurer
	Bound   Bound
	Sleep   Sleep
	Changed func([]*pb.TaskFact)
	Failed  func(*Unreadable)
}

func (f Follower) Follow(ctx context.Context, request *pb.WatchTaskRequest) error {
	var session Session
	var socket Socket
	for attempt := uint(0); ; attempt++ {
		if err := ctx.Err(); err != nil {
			return err
		}
		progressed, err := f.once(ctx, request, &session, &socket)
		session.Disconnected()
		if ctx.Err() != nil {
			return ctx.Err()
		}
		var unreadable *Unreadable
		if !errors.As(err, &unreadable) {
			reason := core.StreamLost
			if status.Code(err) == codes.ResourceExhausted {
				reason = core.MessageTooLarge
			}
			unreadable = &Unreadable{reason, err.Error()}
		}
		f.Failed(unreadable)
		if unreadable.Reason == core.DaemonTooOld || unreadable.Reason == core.ClientTooOld || unreadable.Reason == core.CLINotInstalled || unreadable.Reason == core.MessageTooLarge {
			return unreadable
		}
		if progressed {
			attempt = 0
		}
		if err := f.Sleep(ctx, Backoff(attempt)); err != nil {
			return err
		}
	}
}
func (f Follower) once(ctx context.Context, request *pb.WatchTaskRequest, session *Session, socket *Socket) (bool, error) {
	conn, err := f.connect(ctx, socket)
	if err != nil {
		return false, err
	}
	defer conn.Close()
	token := session.Token()
	r := proto.Clone(request).(*pb.WatchTaskRequest)
	r.Epoch, r.AfterSequence = token.Epoch, token.Sequence
	stream, err := conn.WatchTask(ctx, r)
	if err != nil {
		return false, err
	}
	progressed := false
	for {
		m, err := stream.Recv()
		if err != nil {
			return progressed, err
		}
		changed, err := session.Apply(m)
		if err != nil {
			return progressed, err
		}
		if changed {
			progressed = true
			f.Changed(session.Facts())
		}
	}
}
func (f Follower) connect(ctx context.Context, socket *Socket) (Connection, error) {
	open := func(at Socket) (Connection, error) {
		bound, cancel := f.Bound(ctx)
		defer cancel()
		conn, err := f.Connect(bound, at)
		if err != nil {
			return nil, err
		}
		if err := Hello(bound, conn); err != nil {
			conn.Close()
			return nil, err
		}
		return conn, nil
	}
	reason := core.NoDaemon
	if socket.path != "" {
		conn, err := open(*socket)
		if err == nil {
			return conn, nil
		}
		var unreadable *Unreadable
		if !errors.As(err, &unreadable) || (unreadable.Reason != core.NoDaemon && unreadable.Reason != core.DaemonTooOld) {
			return nil, err
		}
		reason = unreadable.Reason
	}
	bound, cancel := f.Bound(ctx)
	defer cancel()
	answered, err := Ensure(bound, f.Ensurer, reason)
	if err != nil {
		return nil, err
	}
	*socket = answered
	return open(answered)
}
