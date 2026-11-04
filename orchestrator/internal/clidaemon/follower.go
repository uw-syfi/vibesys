package clidaemon

import (
	"context"
	"errors"
	pb "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

// Connection is owned by a follower iteration and closed on every exit path.
type Connection interface {
	Service
	Close() error
}

// Connector locates and dials using the run's recorded location. It returns a
// typed NoDaemon when discovery or dialing fails, without starting anything.
type Connector func(context.Context) (Connection, error)

// Bound derives a unary deadline from the caller, using an injected clock.
type Bound func(context.Context) (context.Context, context.CancelFunc)

// Follower reconnects streams, keeping the last committed Session across cuts.
// Changed receives owned snapshots. Failed marks the view unreadable without
// clearing it. One failed connection may run Ensure once, before reconnecting.
type Follower struct {
	Connect  Connector
	Ensurer  Ensurer
	Location Location
	Bound    Bound
	Sleep    Sleep
	Changed  func([]*pb.TaskFact)
	Failed   func(*Unreadable)
}

func (f Follower) Follow(ctx context.Context, request *pb.WatchTaskRequest) error {
	var session Session
	for attempt := uint(0); ; attempt++ {
		if err := ctx.Err(); err != nil {
			return err
		}
		progressed, err := f.once(ctx, request, &session)
		session.Disconnected()
		if ctx.Err() != nil {
			return ctx.Err()
		}
		var unreadable *Unreadable
		if !errors.As(err, &unreadable) {
			reason := StreamLost
			if status.Code(err) == codes.ResourceExhausted {
				reason = MessageTooLarge
			}
			unreadable = &Unreadable{reason, err.Error()}
		}
		f.Failed(unreadable)
		if unreadable.Reason == DaemonTooOld || unreadable.Reason == ClientTooOld || unreadable.Reason == CLINotInstalled || unreadable.Reason == MessageTooLarge {
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
func (f Follower) once(ctx context.Context, request *pb.WatchTaskRequest, session *Session) (bool, error) {
	conn, err := f.connect(ctx)
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
func (f Follower) connect(ctx context.Context) (Connection, error) {
	open := func() (Connection, error) {
		bound, cancel := f.Bound(ctx)
		defer cancel()
		conn, err := f.Connect(bound)
		if err != nil {
			return nil, err
		}
		if err := Hello(bound, conn); err != nil {
			conn.Close()
			return nil, err
		}
		return conn, nil
	}
	conn, err := open()
	if err == nil {
		return conn, nil
	}
	var unreadable *Unreadable
	if !errors.As(err, &unreadable) || (unreadable.Reason != NoDaemon && unreadable.Reason != DaemonTooOld) {
		return nil, err
	}
	bound, cancel := f.Bound(ctx)
	defer cancel()
	if err := Ensure(bound, f.Ensurer, f.Location, unreadable.Reason); err != nil {
		return nil, err
	}
	return open()
}
