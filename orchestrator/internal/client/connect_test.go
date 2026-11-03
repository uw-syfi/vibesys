package client_test

import (
	"context"
	"fmt"
	"io"
	"math/rand/v2"
	"net"
	"os"
	"runtime"
	"strconv"
	"sync"
	"sync/atomic"
	"testing"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/client"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon/daemontest"
)

// flaky wraps a writer so that bytes pass in small seeded fragments, each after yielding the
// processor, which is the delay a slow pipe gives without a clock; and so that it cuts the pipe
// after a chosen number of bytes.
type flaky struct {
	w       io.WriteCloser
	rng     *rand.Rand
	cutAt   int // bytes to pass before the cut; negative never cuts.
	written int
	mu      sync.Mutex
	onCut   func()
}

func (f *flaky) Write(p []byte) (int, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	total := 0
	for len(p) > 0 {
		n := min(len(p), 1+f.rng.IntN(7))
		if f.cutAt >= 0 && f.written+n > f.cutAt {
			n = f.cutAt - f.written
			if n > 0 {
				w, _ := f.w.Write(p[:n])
				total += w
			}
			f.onCut()
			return total, io.ErrClosedPipe
		}
		runtime.Gosched()
		w, err := f.w.Write(p[:n])
		f.written += w
		total += w
		if err != nil {
			return total, err
		}
		p = p[w:]
	}
	return total, nil
}

func (f *flaky) Close() error { return f.w.Close() }

// connectDialer returns a Dialer that runs client.Connect against the daemon over pipes, as ssh
// would, with faults on the first `faulty` connections.
func connectDialer(live *daemontest.Live, seed uint64, faulty int) client.Dialer {
	rng := rand.New(rand.NewPCG(seed, 7))
	var mu sync.Mutex
	attempt := 0
	return func(ctx context.Context) (net.Conn, error) {
		mu.Lock()
		attempt++
		bad := attempt <= faulty
		cut := -1
		if bad {
			cut = rng.IntN(4000)
		}
		fragSeed := rng.Uint64()
		mu.Unlock()
		// client -> connect (its stdin), and connect (its stdout) -> client.
		stdinR, stdinW := io.Pipe()
		stdoutR, stdoutW := io.Pipe()
		cutBoth := func() { cuts.Add(1); stdinW.CloseWithError(io.ErrClosedPipe); stdoutW.CloseWithError(io.ErrClosedPipe) }
		toDaemon := &flaky{w: stdinW, rng: rand.New(rand.NewPCG(fragSeed, 1)), cutAt: -1, onCut: cutBoth}
		fromDaemon := &flaky{w: stdoutW, rng: rand.New(rand.NewPCG(fragSeed, 2)), cutAt: -1, onCut: cutBoth}
		if bad {
			if cut%2 == 0 {
				toDaemon.cutAt = cut
			} else {
				fromDaemon.cutAt = cut
			}
		}
		go func() {
			// The dial context ends once gRPC has the connection, and the connection outlives it.
			err := client.Connect(context.WithoutCancel(ctx), live.Listener.Dial, stdinR, fromDaemon)
			stdoutW.CloseWithError(err)
		}()
		return client.StdioConn(stdoutR, toDaemon), nil
	}
}

// cuts counts the connections the test broke, so that a test whose faults never fire fails.
var cuts atomic.Int64

func hubSeeds() []uint64 {
	if v := os.Getenv("ORCHESTRATOR_CONNECT_SEED"); v != "" {
		n, err := strconv.ParseUint(v, 10, 64)
		if err != nil {
			panic(err)
		}
		return []uint64{n}
	}
	out := make([]uint64, 25)
	for i := range out {
		out[i] = uint64(i + 1)
	}
	return out
}

// TestAClientSurvivesCutsAndSlowPipesThroughConnect runs commands and a watch through
// `connect` over pipes that deliver a few bytes at a time and break at random. Whatever is cut,
// a retried command takes effect once, and a watch that resumes after its last sequence sees every
// event once and in order.
func TestAClientSurvivesCutsAndSlowPipesThroughConnect(t *testing.T) {
	before := cuts.Load()
	if faultyConnections > 0 && os.Getenv("ORCHESTRATOR_CONNECT_SEED") == "" {
		t.Cleanup(func() {
			if cuts.Load() == before {
				t.Error("no connection was cut, so the test did not exercise a fault")
			}
		})
	}
	for _, seed := range hubSeeds() {
		t.Run(fmt.Sprint("seed", seed), func(t *testing.T) {
			ctx, cancel := context.WithTimeout(context.Background(), guard)
			defer cancel()
			rig := daemontest.NewRig(t)
			live := rig.Serve(t)
			dial := connectDialer(live, seed, faultyConnections)
			fail := func(format string, args ...any) {
				t.Helper()
				t.Fatalf("%s\nreplay: ORCHESTRATOR_CONNECT_SEED=%d go test ./internal/client -run TestAClientSurvivesCutsAndSlowPipesThroughConnect", fmt.Sprintf(format, args...), seed)
			}
			start := &pb.Command{Id: "start-1", Body: &pb.Command_StartRun{StartRun: &pb.StartRun{
				Repository: "/repo", Task: "task", FanoutLimit: 2, Brief: "begin",
				Lead: &pb.AgentSpec{Harness: pb.Harness_HARNESS_CLAUDE_CODE},
			}}}
			// Retry the command on a new connection until it is answered.
			var receipt *pb.Receipt
			for attempt := 0; receipt == nil; attempt++ {
				if attempt > 20 {
					fail("start was never answered")
				}
				c, err := client.Dial(ctx, dial, client.Options{})
				if err != nil {
					continue
				}
				receipt, err = c.Dispatch(ctx, start)
				if err != nil {
					receipt = nil
				}
				c.Close()
			}
			if n := len(rig.Daemon.State().Runs()); n != 1 {
				fail("%d runs after retries of one start, want 1", n)
			}
			// Stop the run the same way: a command retried until it is answered.
			stop := &pb.Command{Id: "stop-1", Body: &pb.Command_StopRun{StopRun: &pb.StopRun{RunId: receipt.GetRunId()}}}
			var stopped *pb.Receipt
			for attempt := 0; stopped == nil; attempt++ {
				if attempt > 20 {
					fail("stop was never answered")
				}
				c, err := client.Dial(ctx, dial, client.Options{})
				if err != nil {
					continue
				}
				if stopped, err = c.Dispatch(ctx, stop); err != nil {
					stopped = nil
				}
				c.Close()
			}
			// Watch across reconnects until the run has ended, resuming after the last sequence read.
			var last uint64
			ended := false
			for attempt := 0; !ended; attempt++ {
				if attempt > 40 {
					fail("never saw the run end")
				}
				c, err := client.Dial(ctx, dial, client.Options{})
				if err != nil {
					continue
				}
				stream, err := c.WatchRuns(ctx, last)
				for err == nil && !ended {
					var msg *pb.WatchRunsResponse
					if msg, err = stream.Recv(); err != nil {
						break
					}
					if s := msg.GetSnapshot(); s != nil {
						last = s.GetSequence()
						for _, r := range s.GetRuns() {
							ended = ended || r.GetStatus() == pb.RunStatus_RUN_STATUS_ENDED
						}
						continue
					}
					if msg.GetSearch() != nil {
						// A whole search view carries no sequence and does not advance the event stream.
						// Search delivery: daemon.TestSearchWatchConvergesAcrossLagAndReconnect
						// and hub.TestLatestThousandPublishesAndLag.
						continue
					}
					ev := msg.GetEvent()
					if last != 0 && ev.GetSequence() != last+1 {
						fail("event %d came after %d: a gap or a repeat", ev.GetSequence(), last)
					}
					last = ev.GetSequence()
					r := ev.GetRunUpdated()
					ended = r != nil && r.GetStatus() == pb.RunStatus_RUN_STATUS_ENDED
				}
				c.Close()
			}
			if n := len(rig.Daemon.State().Runs()); n != 1 {
				fail("%d runs after retries of one start, want 1", n)
			}
		})
	}
}

var faultyConnections = func() int {
	if v := os.Getenv("ORCHESTRATOR_CONNECT_FAULTS"); v != "" {
		n, _ := strconv.Atoi(v)
		return n
	}
	return 3
}()
