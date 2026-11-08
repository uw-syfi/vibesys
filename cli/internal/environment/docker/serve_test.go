package docker_test

import (
	"bytes"
	"context"
	"fmt"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/envcodec"
	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker/dockerfake"
	"github.com/uw-syfi/vibesys/cli/internal/resource"
	"github.com/uw-syfi/vibesys/cli/internal/resource/fakeresource"
)

// jobServer is a job-scoped server of the task's image that runs serve.
func jobServer(t *testing.T, serve ...string) core.JobService {
	return core.JobService{Checkout: checkout(t), Argv: serve, Ready: "/health", StartupTimeout: startup, Lifetime: 3 * time.Hour}
}

// serve starts the server with the probes pumped, and returns it and how its start ended.
func (s *candidateScene) serve(ctx context.Context, js core.JobService, out *syncBuffer) (environment.Service, core.Attempt) {
	pumping, stop := context.WithCancel(context.Background())
	var wg sync.WaitGroup
	wg.Go(func() { s.pump(pumping) })
	sv, a := s.env.Serve(ctx, js, out)
	stop()
	wg.Wait()
	return sv, a
}

// syncBuffer is an output the server and a command write to at once.
type syncBuffer struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (b *syncBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.Write(p)
}

func (b *syncBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.String()
}

// named returns the container whose name ends with suffix, or the one that ends with neither
// suffix of a server's container for "".
func named(t *testing.T, boxes []dockerfake.Info, suffix string) dockerfake.Info {
	t.Helper()
	for _, b := range boxes {
		if suffix != "" && strings.HasSuffix(b.Name, suffix) || suffix == "" && !strings.HasSuffix(b.Name, "-service") && !strings.HasSuffix(b.Name, "-candidate") {
			return b
		}
	}
	t.Fatalf("no container %q among %+v", suffix, boxes)
	return dockerfake.Info{}
}

// The server's container is started once, with the GPUs and the probe, and outlives each run; every
// run's command runs in a container of its own, joined to the server's network, with its address
// and without the GPUs, which is removed when the run ends; Close removes the server's.
func TestAJobServerStartsOnceAndEveryRunJoinsItsNetwork(t *testing.T) {
	s := newCandidateScene(t)
	out := &syncBuffer{}
	sv, a := s.serve(context.Background(), jobServer(t, "print", "serving on ${PORT}", ";", "ready", ";", "hold", "forever"), out)
	if a != nil {
		t.Fatalf("start %+v", a)
	}
	boxes := s.daemon.Containers()
	if len(boxes) != 1 {
		t.Fatalf("containers %+v, want the server's", boxes)
	}
	server := named(t, boxes, "-service")
	if server.Spec.GPUs.Devices != "device=0" || server.Spec.Env["FAKE_VISIBLE_DEVICES"] != "0" || !server.Spec.Measured {
		t.Errorf("the server lacks the GPUs or the whole machine: %+v", server.Spec)
	}
	for i := range 2 {
		running, goOn := fmt.Sprintf("running-%d", i), fmt.Sprintf("go-%d", i)
		x := core.Execution{Checkout: checkout(t), Argv: []string{"mark", running, ";", "hold", goOn, ";", "print", "load done"}, Timeout: limit, Measured: true}
		var (
			attempt core.Attempt
			wg      sync.WaitGroup
		)
		wg.Go(func() { attempt = sv.Run(context.Background(), x, out) })
		s.reach(running)
		boxes := s.daemon.Containers()
		if len(boxes) != 2 {
			t.Fatalf("run %d: containers %+v, want the server's and the command's", i, boxes)
		}
		command := named(t, boxes, "")
		if command.Spec.Network != "container:"+server.Name {
			t.Errorf("run %d: the command's network is %q, want the server's", i, command.Spec.Network)
		}
		if command.Spec.GPUs != (docker.GPUs{}) || command.Spec.Env["FAKE_VISIBLE_DEVICES"] != "" {
			t.Errorf("run %d: the command's container has GPUs: %+v", i, command.Spec)
		}
		var job cliv1.JobEnvironment
		if err := envcodec.Decode(func(n string) string { return command.Spec.Env[n] }, &job); err != nil {
			t.Fatal(err)
		}
		port := strings.TrimPrefix(job.GetServiceUrl(), "http://127.0.0.1:")
		if port == job.GetServiceUrl() || !strings.Contains(out.String(), "serving on "+port) {
			t.Errorf("run %d: the command is told %q, and the server said %q", i, job.GetServiceUrl(), out.String())
		}
		s.world.Release(goOn)
		wg.Wait()
		if !isExit0(attempt) || !sv.Alive() {
			t.Fatalf("run %d: attempt %+v, server alive %v", i, attempt, sv.Alive())
		}
		if left := s.daemon.Containers(); len(left) != 1 || left[0].Name != server.Name {
			t.Fatalf("run %d: containers %+v after the run, want the server's alone", i, left)
		}
	}
	if err := sv.Close(); err != nil {
		t.Fatal(err)
	}
	if err := sv.Close(); err != nil {
		t.Fatalf("a second Close: %v", err)
	}
	if running := s.daemon.Running(); len(running) != 0 {
		t.Fatalf("still running: %v", running)
	}
	s.noContainersLeft()
}

// Every start of a server that fails removes its container before Serve returns, and says why
// with the server's last lines.
func TestEveryFailedStartOfAJobServerRemovesItsContainer(t *testing.T) {
	cases := []struct {
		name  string
		serve []string
		act   func(s *candidateScene, cancel func())
		kind  core.ExecKind
		log   string
	}{
		{
			name: "never ready", serve: []string{"print", "loading weights", ";", "mark", "serving", ";", "hold", "forever"},
			act:  func(s *candidateScene, _ func()) { s.reach("serving"); s.fire(startup) },
			kind: core.ExecServiceNotReady, log: "loading weights",
		},
		{name: "exits early", serve: []string{"print", "out of memory", ";", "exit", "3"}, kind: core.ExecServiceExited, log: "out of memory"},
		{
			name: "a cancel while it loads", serve: []string{"mark", "serving", ";", "hold", "forever"},
			act:  func(s *candidateScene, cancel func()) { s.reach("serving"); cancel() },
			kind: core.ExecCancelled,
		},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			s := newCandidateScene(t)
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			var (
				sv environment.Service
				a  core.Attempt
				wg sync.WaitGroup
			)
			wg.Go(func() { sv, a = s.serve(ctx, jobServer(t, c.serve...), &syncBuffer{}) })
			if c.act != nil {
				c.act(s, cancel)
			}
			wg.Wait()
			e, ok := a.(core.Ended)
			if sv != nil || !ok || e.Exec.Kind != c.kind || !strings.Contains(e.ServiceLog, c.log) {
				t.Fatalf("server %v, attempt %+v, want %s with %q", sv, a, c.kind, c.log)
			}
			s.noContainersLeft()
		})
	}
}

// A server that ends while a command runs against it ends the run as the server's exit, with its
// last lines; it is then not alive, a run against it runs nothing, and Close still removes it.
func TestARunAgainstAServerThatEndsIsTheServersExit(t *testing.T) {
	s := newCandidateScene(t)
	sv, a := s.serve(context.Background(), jobServer(t, "ready", ";", "hold", "crash", ";", "print", "segfault", ";", "exit", "139"), &syncBuffer{})
	if a != nil {
		t.Fatalf("start %+v", a)
	}
	var (
		attempt core.Attempt
		wg      sync.WaitGroup
	)
	wg.Go(func() {
		attempt = sv.Run(context.Background(), core.Execution{Checkout: checkout(t), Argv: []string{"mark", "running", ";", "hold", "forever"}, Timeout: limit}, nil)
	})
	s.reach("running")
	s.world.Release("crash")
	wg.Wait()
	if e, ok := attempt.(core.Ended); !ok || e.Exec.Kind != core.ExecServiceExited || !strings.Contains(e.ServiceLog, "segfault") {
		t.Fatalf("attempt %+v", attempt)
	}
	if sv.Alive() {
		t.Fatal("an ended server is alive")
	}
	after := sv.Run(context.Background(), core.Execution{Checkout: checkout(t), Argv: []string{"exit", "0"}, Timeout: limit}, nil)
	if ns, ok := after.(core.NotStarted); !ok || ns.Failure != environment.ServiceEnded() {
		t.Fatalf("a run against an ended server: %+v", after)
	}
	if err := sv.Close(); err != nil {
		t.Fatal(err)
	}
	s.noContainersLeft()
}

// A server whose caller died stops with it, as any container does, and the next run of the scope
// removes it.
func TestTheServerOfACallerThatDiedIsRemovedByTheNextRun(t *testing.T) {
	s := newCandidateScene(t)
	sv, a := s.serve(context.Background(), jobServer(t, "ready", ";", "hold", "forever"), &syncBuffer{})
	if a != nil {
		t.Fatalf("start %+v", a)
	}
	if len(s.daemon.Running()) != 1 || !sv.Alive() {
		t.Fatalf("the server does not run: %v", s.daemon.Running())
	}
	s.r.client.Crash()
	next := newRig(t, s.daemon, options{process: 2})
	if attempt, _ := (&scene{t: t, world: s.world, daemon: s.daemon, r: next, env: next.env(t)}).run("print", "x"); !isExit0(attempt) {
		t.Fatalf("attempt %+v", attempt)
	}
	s.noContainersLeft()
}

// A placement whose resources choose the network gives it to the server, whose network each
// command joins: a cluster's network reaches the commands that drive it.
func TestAResourcesNetworkIsTheServersAndTheCommandsJoinIt(t *testing.T) {
	cluster := &fakeresource.Fake{
		Of: core.ResourceKubernetes, Facts: map[string]string{"kubernetes_id": "k"},
		Attached: resource.Attachment{Env: map[string]string{"KUBECONFIG": "/kube/config"}, Mounts: []resource.Mount{{Host: "/kube", Container: "/kube", ReadOnly: true}}, Network: "kind"},
		Claimed:  []core.Claim{{Resource: "kubernetes:k", Amount: 1}},
	}
	s := newCandidateScene(t, cluster)
	sv, a := s.serve(context.Background(), jobServer(t, "ready", ";", "hold", "forever"), &syncBuffer{})
	if a != nil {
		t.Fatalf("start %+v", a)
	}
	defer func() { _ = sv.Close() }()
	server := named(t, s.daemon.Containers(), "-service")
	if server.Spec.Network != "kind" {
		t.Errorf("the server's network is %q, want the resource's", server.Spec.Network)
	}
	var (
		attempt core.Attempt
		wg      sync.WaitGroup
	)
	wg.Go(func() {
		attempt = sv.Run(context.Background(), core.Execution{Checkout: checkout(t), Argv: []string{"mark", "running", ";", "hold", "go"}, Timeout: limit}, nil)
	})
	s.reach("running")
	command := named(t, s.daemon.Containers(), "")
	if command.Spec.Network != "container:"+server.Name || command.Spec.Env["KUBECONFIG"] != "/kube/config" {
		t.Errorf("the command's container: %+v", command.Spec)
	}
	s.world.Release("go")
	wg.Wait()
	if !isExit0(attempt) {
		t.Fatalf("attempt %+v", attempt)
	}
}

// A placement with no probe runs no job-scoped server, and starts nothing.
func TestAPlacementWithoutAProbeRunsNoJobServer(t *testing.T) {
	s := newScene(t, options{})
	sv, a := s.env.Serve(context.Background(), jobServer(t, "ready"), nil)
	if ns, ok := a.(core.NotStarted); sv != nil || !ok || ns.Failure.Kind != core.FailureUnavailable || !strings.Contains(ns.Failure.Message, "[service]") {
		t.Fatalf("server %v, attempt %+v", sv, a)
	}
	if left := s.daemon.Containers(); len(left) != 0 {
		t.Fatalf("containers %+v", left)
	}
}

// What cannot be a server, or a command against one, is refused before anything starts.
func TestWhatCannotBeAJobServerIsRefusedBeforeAnythingStarts(t *testing.T) {
	s := newCandidateScene(t)
	for name, change := range map[string]func(*core.JobService){
		"no command":           func(j *core.JobService) { j.Argv = nil },
		"a relative checkout":  func(j *core.JobService) { j.Checkout.Dir = "work" },
		"a readiness not path": func(j *core.JobService) { j.Ready = "health" },
		"no startup time":      func(j *core.JobService) { j.StartupTimeout = 0 },
		"no lifetime":          func(j *core.JobService) { j.Lifetime = 0 },
		"artifacts inside":     func(j *core.JobService) { j.Artifacts = j.Checkout.Dir + "/out" },
	} {
		js := jobServer(t, "ready")
		change(&js)
		if sv, a := s.env.Serve(context.Background(), js, nil); sv != nil || a == nil || a.(core.NotStarted).Failure.Kind != core.FailureInvalidInput {
			t.Errorf("%s: server %v, attempt %+v", name, sv, a)
		}
	}
	sv, a := s.serve(context.Background(), jobServer(t, "ready", ";", "hold", "forever"), &syncBuffer{})
	if a != nil {
		t.Fatalf("start %+v", a)
	}
	defer func() { _ = sv.Close() }()
	x := core.Execution{Checkout: checkout(t), Argv: []string{"exit", "0"}, Timeout: limit, Service: &core.ServiceSpec{Argv: []string{"ready"}}}
	if a, ok := sv.Run(context.Background(), x, nil).(core.NotStarted); !ok || a.Failure.Kind != core.FailureInvalidInput {
		t.Fatalf("a command with a service of its own: %+v", a)
	}
	if len(s.daemon.Containers()) != 1 {
		t.Fatalf("containers %+v", s.daemon.Containers())
	}
}
