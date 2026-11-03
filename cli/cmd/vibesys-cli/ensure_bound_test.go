package main

import (
	"bytes"
	"context"
	"net"
	"syscall"
	"testing"
	"time"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/client"
	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"google.golang.org/grpc"
	"google.golang.org/grpc/test/bufconn"
)

type ensurePeer struct {
	cliv1.UnimplementedDaemonServiceServer
	stage    string
	entered  chan struct{}
	listener *bufconn.Listener
	released bool
}

func (p *ensurePeer) wait(ctx context.Context) error {
	close(p.entered)
	<-ctx.Done()
	return ctx.Err()
}
func (p *ensurePeer) Dial(ctx context.Context) (net.Conn, error) {
	if p.stage == "dial" {
		return nil, p.wait(ctx)
	}
	if p.stage == "lock" || p.stage == "ready" {
		return nil, syscall.ECONNREFUSED
	}
	return p.listener.DialContext(ctx)
}
func (p *ensurePeer) HoldStart(ctx context.Context) (func() error, error) {
	if p.stage == "lock" {
		return nil, p.wait(ctx)
	}
	return func() error { p.released = true; return nil }, nil
}
func (p *ensurePeer) AwaitExit(ctx context.Context) error { <-ctx.Done(); return ctx.Err() }
func (p *ensurePeer) Start(ctx context.Context) error     { return p.wait(ctx) }
func (p *ensurePeer) Hello(ctx context.Context, _ *cliv1.HelloRequest) (*cliv1.HelloResponse, error) {
	if p.stage == "hello" {
		return nil, p.wait(ctx)
	}
	answer := cliv1.Compatibility_COMPATIBILITY_COMPATIBLE
	if p.stage == "stop" {
		answer = cliv1.Compatibility_COMPATIBILITY_DAEMON_TOO_OLD
	}
	return &cliv1.HelloResponse{Compatibility: answer}, nil
}
func (p *ensurePeer) Stop(ctx context.Context, _ *cliv1.StopRequest) (*cliv1.StopResponse, error) {
	return nil, p.wait(ctx)
}
func (p *ensurePeer) Status(ctx context.Context, _ *cliv1.StatusRequest) (*cliv1.StatusResponse, error) {
	return nil, p.wait(ctx)
}

func TestEnsureDeadlineCoversEveryBlockingBoundary(t *testing.T) {
	for _, stage := range []string{"dial", "lock", "hello", "stop", "ready", "status"} {
		t.Run(stage, func(t *testing.T) {
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			p := &ensurePeer{stage: stage, entered: make(chan struct{}), listener: bufconn.Listen(1 << 20)}
			server := grpc.NewServer()
			cliv1.RegisterDaemonServiceServer(server, p)
			go server.Serve(p.listener)
			defer server.Stop()
			timers := clock.NewManualTimers()
			var stderr bytes.Buffer
			d := deps{getenv: func(string) string { return "" }, stdout: &bytes.Buffer{}, stderr: &stderr, options: func() (client.Options, error) {
				return client.Options{Rendezvous: p, Starter: p, Timers: timers}, nil
			}}
			done := make(chan int, 1)
			go func() { done <- daemonEnsure(ctx, d, []string{"--json"}) }()
			// The real deadline below only guards a broken test against a hang.
			guard, stop := context.WithTimeout(context.Background(), 10*time.Second)
			defer stop()
			select {
			case <-p.entered:
			case <-guard.Done():
				t.Fatal("boundary not reached")
			}
			timers.Fire()
			select {
			case code := <-done:
				if code != exitNoAnswer || !bytes.Contains(stderr.Bytes(), []byte("timed out")) {
					t.Fatalf("code %d: %s", code, &stderr)
				}
			case <-guard.Done():
				t.Fatal("ensure did not end")
			}
			if (stage == "stop" || stage == "ready") && !p.released {
				t.Fatal("startup ownership leaked")
			}
		})
	}
}
