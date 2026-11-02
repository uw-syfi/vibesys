package docker_test

import (
	"context"
	"fmt"
	"math/rand/v2"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker/dockerfake"
	"github.com/uw-syfi/vibesys/cli/internal/environment/envtest"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv/fakeworld"
)

// sim is a seeded simulation of several processes that run jobs in turn against one daemon whose
// every call can fail, before it does its work or after, and whose processes die.
type sim struct {
	t     *testing.T
	seed  uint64
	rng   *rand.Rand // the scenario's choices; only the test's goroutine uses it
	world fakeworld.World
	d     *dockerfake.Daemon

	faultMu  sync.Mutex
	faultRng *rand.Rand
	rate     float64
}

func (s *sim) faults(c dockerfake.Call) *dockerfake.Fault {
	s.faultMu.Lock()
	defer s.faultMu.Unlock()
	if s.faultRng.Float64() >= s.rate {
		return nil
	}
	return &dockerfake.Fault{Err: boom, Late: s.faultRng.IntN(2) == 0}
}

// until waits for ok to hold, and gives up when done is closed: a run that failed before it got
// to the point the test waits for must not hang the test.
func (s *sim) until(done <-chan struct{}, ok func(context.Context) bool) bool {
	ctx, cancel := context.WithTimeout(context.Background(), envtest.Backstop)
	defer cancel()
	stop := make(chan struct{})
	var wg sync.WaitGroup
	wg.Go(func() {
		select {
		case <-done:
			cancel()
		case <-stop:
		}
	})
	got := ok(ctx)
	close(stop)
	wg.Wait()
	return got
}

type scenario string

const (
	scenQuick   scenario = "a command that exits"
	scenService scenario = "a command with a service"
	scenCancel  scenario = "a cancellation"
	scenCrash   scenario = "the death of the process"
	scenLimit   scenario = "the limit"
)

var scenarios = []scenario{scenQuick, scenService, scenCancel, scenCrash, scenLimit}

// step runs one job in a process of its own and checks what it left behind.
func (s *sim) step(i int, process uint32, sc scenario) {
	t := s.t
	timers := envtest.NewTimers()
	r := newRig(t, s.d, options{process: process, timers: timers})
	e := r.env(t)
	point := fmt.Sprintf("up-%d-%d", s.seed, i)
	gate := fmt.Sprintf("go-%d-%d", s.seed, i)
	x := core.Execution{Checkout: checkout(t), Timeout: time.Hour}
	switch sc {
	case scenQuick:
		x.Argv = []string{"print", "x"}
	case scenService:
		x.Argv = []string{"print", "x"}
		x.Service = &core.ServiceSpec{Argv: []string{"hold", "forever"}}
	default:
		x.Argv = []string{"mark", point, ";", "hold", gate}
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	var (
		outcome core.ExecOutcome
		err     error
		wg      sync.WaitGroup
	)
	done := make(chan struct{})
	wg.Go(func() {
		defer close(done)
		outcome, err = e.Run(ctx, x, nil)
	})
	switch sc {
	case scenCancel:
		if s.until(done, func(c context.Context) bool { return s.world.Reached(c, point) }) {
			cancel()
		}
	case scenCrash:
		if s.until(done, func(c context.Context) bool { return s.world.Reached(c, point) }) {
			r.client.Crash()
		}
	case scenLimit:
		if s.until(done, func(c context.Context) bool { return timers.Await(c, 1) != nil && s.world.Reached(c, point) }) {
			timers.Await(context.Background(), 1).Fire()
		}
	}
	wg.Wait()

	prefix := fmt.Sprintf("vibesys-cli-%08x", process)
	where := fmt.Sprintf("seed %d step %d (%s), outcome %+v, err %v", s.seed, i, sc, outcome, err)
	if running := s.d.Running(); len(running) != 0 {
		t.Fatalf("%s: containers %v still run after the run returned", where, running)
	}
	for _, c := range s.d.Containers() {
		if !strings.HasPrefix(c.Name, prefix) {
			continue
		}
		// A container this run made may stay only if the run says so, by name.
		if err == nil || !strings.Contains(err.Error(), c.Name) {
			t.Fatalf("%s: the run's container %s is left and the run does not say so", where, c.Name)
		}
	}
	if err == nil && outcome.Kind == "" {
		t.Fatalf("%s: neither an outcome nor an error", where)
	}
}

func TestNothingIsLeftBehindWhicheverCallFailsOrProcessDies(t *testing.T) {
	for seed := range uint64(150) {
		w := fakeworld.New()
		s := &sim{
			t: t, seed: seed, rng: rand.New(rand.NewPCG(seed, 1)), world: w, d: dockerfake.NewDaemon(w.MemoryGates),
			faultRng: rand.New(rand.NewPCG(seed, 2)), rate: []float64{0, 0.1, 0.3}[seed%3],
		}
		s.d.SetFaults(s.faults)
		for i := range 6 {
			s.step(i, uint32(i+1), scenarios[s.rng.IntN(len(scenarios))])
		}
		// The next start: with the engine working, the first run clears whatever earlier
		// processes left, and then leaves nothing of its own.
		s.d.SetFaults(nil)
		r := newRig(t, s.d, options{process: 99})
		out, err := r.env(t).Run(context.Background(), core.Execution{Checkout: checkout(t), Argv: []string{"print", "x"}, Timeout: time.Hour}, nil)
		if err != nil || out.Kind != core.ExecExited {
			t.Fatalf("seed %d: the run after the failures: %+v, %v", seed, out, err)
		}
		if left := s.d.Containers(); len(left) != 0 {
			t.Fatalf("seed %d: %d containers left after a clean run: %+v", seed, len(left), left)
		}
	}
}

// The death of a process leaves its container stopped, and the next process removes it before it
// runs anything.
func TestALostSupervisorsContainerStopsAndTheNextRunRemovesIt(t *testing.T) {
	s := newScene(t, options{})
	first := s.r
	x := core.Execution{Checkout: checkout(t), Argv: []string{"alive", "job", ";", "mark", "up", ";", "hold", "forever"}, Timeout: time.Hour}
	var wg sync.WaitGroup
	wg.Go(func() { _, _ = first.env(t).Run(context.Background(), x, nil) })
	ctx, cancel := context.WithTimeout(context.Background(), envtest.Backstop)
	defer cancel()
	if !s.world.Reached(ctx, "up") {
		t.Fatal("the job never started")
	}
	first.client.Crash()
	if !s.world.Gone(ctx, "job") {
		t.Fatal("the job still runs after its supervisor died")
	}
	wg.Wait()

	next := newRig(t, s.daemon, options{process: 2})
	e := next.env(t)
	if _, err := e.Run(context.Background(), core.Execution{Checkout: checkout(t), Argv: []string{"print", "x"}, Timeout: time.Hour}, nil); err != nil {
		t.Fatal(err)
	}
	if left := s.daemon.Containers(); len(left) != 0 {
		t.Fatalf("containers %+v left after the next run", left)
	}
}

var _ = docker.Owner
