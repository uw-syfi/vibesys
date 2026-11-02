package daemontest

import (
	"context"
	"errors"
	"log/slog"
	"net"
	"sync"
	"testing"
	"time"

	"google.golang.org/grpc"

	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
)

// PipeListener is a net.Listener whose connections are in-memory pipes: Dial is the client's
// Dialer, and Accept hands the other end to the gRPC server.
type PipeListener struct {
	conns  chan net.Conn
	closed chan struct{}
	once   sync.Once
}

// NewPipeListener returns a listener with no connections.
func NewPipeListener() *PipeListener {
	return &PipeListener{conns: make(chan net.Conn), closed: make(chan struct{})}
}

// Accept implements net.Listener.
func (l *PipeListener) Accept() (net.Conn, error) {
	select {
	case c := <-l.conns:
		return c, nil
	case <-l.closed:
		return nil, net.ErrClosed
	}
}

// Close implements net.Listener.
func (l *PipeListener) Close() error {
	l.once.Do(func() { close(l.closed) })
	return nil
}

type pipeAddr struct{}

func (pipeAddr) Network() string { return "pipe" }
func (pipeAddr) String() string  { return "orchestrator" }

// Addr implements net.Listener.
func (l *PipeListener) Addr() net.Addr { return pipeAddr{} }

// Dial returns the client end of a new connection.
func (l *PipeListener) Dial(ctx context.Context) (net.Conn, error) {
	client, server := net.Pipe()
	select {
	case l.conns <- server:
		return client, nil
	case <-l.closed:
		return nil, net.ErrClosed
	case <-ctx.Done():
		return nil, ctx.Err()
	}
}

// Live is a Rig serving over a PipeListener, with its daemon loop running in a goroutine. The loop
// does the work the test would otherwise do with Settle, so a test that talks to it as a client
// waits for events and not for time.
type Live struct {
	*Rig
	// Listener is the operator's socket and AgentListener the agent-only socket.
	Listener      *PipeListener
	AgentListener *PipeListener
	ticks         chan time.Time
	stop          func()
}

// Serve starts the gRPC server and the daemon loop. The test's cleanup stops them.
func (r *Rig) Serve(tb testing.TB) *Live {
	tb.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	l := &Live{Rig: r, Listener: NewPipeListener(), AgentListener: NewPipeListener(), ticks: make(chan time.Time)}
	var servers []*grpc.Server
	var wg sync.WaitGroup
	wg.Add(3)
	for _, s := range []struct {
		service  *daemon.Service
		listener *PipeListener
	}{{r.Service, l.Listener}, {r.AgentService, l.AgentListener}} {
		server := grpc.NewServer()
		s.service.Register(server)
		servers = append(servers, server)
		go func() {
			defer wg.Done()
			if err := server.Serve(s.listener); err != nil && !errors.Is(err, grpc.ErrServerStopped) {
				tb.Errorf("serve: %v", err)
			}
		}()
	}
	go func() {
		defer wg.Done()
		r.Daemon.Run(ctx, l.ticks, r.Wake, slog.New(slog.DiscardHandler))
	}()
	l.stop = func() {
		cancel()
		for _, server := range servers {
			server.Stop()
		}
		_ = l.Listener.Close()
		_ = l.AgentListener.Close()
		wg.Wait()
	}
	tb.Cleanup(l.Stop)
	r.Wake <- struct{}{} // the first pass starts whatever the state already holds.
	return l
}

// Stop ends the server and the loop, as a crash would. It is safe to call twice.
func (l *Live) Stop() { l.stop() }

// Tick ends the hub's merge window and steps once more. It returns after the loop took the tick.
func (l *Live) Tick() { l.ticks <- time.Time{} }
