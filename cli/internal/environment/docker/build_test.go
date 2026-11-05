package docker_test

import (
	"bytes"
	"context"
	"strings"
	"sync"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker/dockerfake"
	"github.com/uw-syfi/vibesys/cli/internal/environment/envtest"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv/fakeworld"
	"github.com/uw-syfi/vibesys/cli/internal/lease"
	"github.com/uw-syfi/vibesys/cli/internal/lease/fakelease"
)

// imageKind is one of the images a docker environment builds: the task's, which a Fingerprint
// builds, and a candidate's.
type imageKind struct {
	name string
	// ask builds the image, or finds it built, and returns its ID.
	ask func(ctx context.Context, e environment.Environment, log *bytes.Buffer) (string, error)
}

var imageKinds = []imageKind{
	{"task", func(ctx context.Context, e environment.Environment, _ *bytes.Buffer) (string, error) {
		f, err := e.Fingerprint(ctx)
		return f.ImageDigest, err
	}},
	{"candidate", func(ctx context.Context, e environment.Environment, log *bytes.Buffer) (string, error) {
		img, err := e.Candidate(ctx, build, log)
		return img.ID, err
	}},
}

// processes are two processes of one machine, each with its own docker environment over one
// daemon whose builds wait at gates, building with the leases builds gives each.
type processes struct {
	daemon *dockerfake.Daemon
	world  fakeworld.World
	timers *envtest.Timers
	envs   [2]environment.Environment
}

func newProcesses(t *testing.T, builds func() lease.Leases) *processes {
	t.Helper()
	w := fakeworld.New()
	d := dockerfake.NewDaemon(w.MemoryGates, dockerfake.HeldBuilds(), dockerfake.AttestedBuilds())
	ps := &processes{daemon: d, world: w, timers: envtest.NewTimers()}
	for i := range ps.envs {
		files := &candidateFiles{files: map[string]string{"serve/Dockerfile": "FROM busybox:1.36.1\nCOPY . /srv\n"}}
		r := newRig(t, d, options{process: uint32(i + 1), timers: ps.timers, builds: builds(), configure: func(c *docker.Config) {
			c.Candidates = files
			c.Holder = core.LeaseHolder{User: "alice", PID: 100 + i}
		}})
		ps.envs[i] = r.env(t)
	}
	return ps
}

// answer is what one process's ask returned.
type answer struct {
	process int
	id      string
	err     error
	log     string
}

// askBoth asks both processes for the image at once, and returns a channel of their answers in
// the order they come.
func (ps *processes) askBoth(ctx context.Context, kind imageKind) <-chan answer {
	out := make(chan answer, len(ps.envs))
	for i, e := range ps.envs {
		go func() {
			var log bytes.Buffer
			id, err := kind.ask(ctx, e, &log)
			out <- answer{process: i, id: id, err: err, log: log.String()}
		}()
	}
	return out
}

func next(t *testing.T, ctx context.Context, answers <-chan answer) answer {
	t.Helper()
	select {
	case a := <-answers:
		if a.err != nil {
			t.Fatalf("process %d: %v", a.process, a.err)
		}
		return a
	case <-ctx.Done():
		t.Fatal("a process never answered")
		return answer{}
	}
}

// Two jobs of one commit started together, each in its own supervisor process, both built one
// image: the build that ended second failed with Docker's "already exists" and failed its job. A
// build that loses that race to a process it shares no lease with (another machine's, another
// daemon's of a different scope) now uses the image the winner made. The builds are released in
// the order that makes the one started first end last, whichever process started first.
func TestABuildThatLosesARaceForItsTagUsesTheWinnersImage(t *testing.T) {
	for _, kind := range imageKinds {
		t.Run(kind.name, func(t *testing.T) {
			ctx, cancel := context.WithTimeout(context.Background(), envtest.Backstop)
			defer cancel()
			// Each process takes its leases at a place of its own: nothing keeps their builds apart.
			ps := newProcesses(t, func() lease.Leases { return (&fakelease.Place{}).Daemon() })
			answers := ps.askBoth(ctx, kind)
			ps.world.Release(dockerfake.BuildGate(2))
			won := next(t, ctx, answers)
			ps.world.Release(dockerfake.BuildGate(1))
			lost := next(t, ctx, answers)
			if won.id != lost.id || won.id == "" {
				t.Fatalf("the processes got the images %q and %q, want one", won.id, lost.id)
			}
			if n := ps.daemon.Builds(); n != 2 {
				t.Fatalf("%d builds, want 2: each process found no image and built", n)
			}
			if kind.name == "candidate" && !strings.Contains(lost.log, "already exists") {
				t.Fatalf("the losing build's log does not say why its build failed:\n%s", lost.log)
			}
		})
	}
}

// Two processes of one machine take turns at an image's build under the lease of its tag: the
// one that finds it held waits, then uses the image the holder built, and builds nothing, so one
// commit's jobs get one image.
func TestProcessesThatShareTheBuildLeasesBuildAnImageOnce(t *testing.T) {
	for _, kind := range imageKinds {
		t.Run(kind.name, func(t *testing.T) {
			ctx, cancel := context.WithTimeout(context.Background(), envtest.Backstop)
			defer cancel()
			machine := &fakelease.Place{Name: "this machine"}
			ps := newProcesses(t, func() lease.Leases { return machine.Daemon() })
			answers := ps.askBoth(ctx, kind)
			// The one that did not take the lease waits a pause to try again.
			var poll *envtest.Timer
			for n := 1; poll == nil; n++ {
				timer := ps.timers.Await(ctx, n)
				if timer == nil {
					t.Fatal("no process waited for the other's build")
				}
				if timer.Duration == docker.BuildPoll {
					poll = timer
				}
			}
			ps.world.Release(dockerfake.BuildGate(1))
			built := next(t, ctx, answers)
			poll.Fire()
			waited := next(t, ctx, answers)
			if built.id != waited.id || built.id == "" {
				t.Fatalf("the processes got the images %q and %q, want one", built.id, waited.id)
			}
			if n := ps.daemon.Builds(); n != 1 {
				t.Fatalf("%d builds, want 1", n)
			}
			if held := machine.Held(); len(held) != 0 {
				t.Fatalf("leases still held after both answered: %v", held)
			}
			if kind.name == "candidate" && !strings.Contains(waited.log, "alice (pid 10") {
				t.Fatalf("the waiting process's log does not name who builds:\n%s", waited.log)
			}
		})
	}
}

// The lease is given back on every path: a build that fails, and a wait cut short, leave none
// held, and the next ask builds.
func TestABuildsLeaseIsGivenBackWhenItFailsOrIsCancelled(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), envtest.Backstop)
	defer cancel()
	machine := &fakelease.Place{}
	ps := newProcesses(t, func() lease.Leases { return machine.Daemon() })
	ps.daemon.SetFaults(func(c dockerfake.Call) *dockerfake.Fault {
		if c.Op == dockerfake.OpBuild {
			return &dockerfake.Fault{Err: boom}
		}
		return nil
	})
	if _, err := ps.envs[0].Fingerprint(ctx); err == nil {
		t.Fatal("a failed build gave an image")
	}
	if held := machine.Held(); len(held) != 0 {
		t.Fatalf("a failed build left its lease held: %v", held)
	}
	// The first process holds the lease in its build; the second waits, and is cancelled.
	ps.daemon.SetFaults(func(c dockerfake.Call) *dockerfake.Fault {
		if c.Op == dockerfake.OpBuild {
			ps.world.Mark("building")
		}
		return nil
	})
	var wg sync.WaitGroup
	var first error
	wg.Go(func() { _, first = ps.envs[0].Fingerprint(ctx) })
	if !ps.world.Reached(ctx, "building") {
		t.Fatal("the first process never built")
	}
	waiting, stop := context.WithCancel(ctx)
	var second error
	wg.Go(func() { _, second = ps.envs[1].Fingerprint(waiting) })
	if ps.timers.Await(ctx, 1) == nil {
		t.Fatal("the second process never waited")
	}
	stop()
	ps.world.Release(dockerfake.BuildGate(1))
	wg.Wait()
	if first != nil || second == nil {
		t.Fatalf("the holder: %v, the cancelled waiter: %v; want an image and an error", first, second)
	}
	if held := machine.Held(); len(held) != 0 {
		t.Fatalf("leases still held: %v", held)
	}
}
