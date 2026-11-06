package docker_test

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker/dockerfake"
	"github.com/uw-syfi/vibesys/cli/internal/environment/envtest"
	"github.com/uw-syfi/vibesys/cli/internal/resource"
	"github.com/uw-syfi/vibesys/cli/internal/resource/fakeresource"
	"github.com/uw-syfi/vibesys/cli/internal/task"
)

// candidateFiles is a CandidateSource over files a test gives: the candidate's commit, as the
// daemon reads it.
type candidateFiles struct {
	mu    sync.Mutex
	files map[string]string // by the Dockerfile's path: its text
}

func (c *candidateFiles) Candidate(_ context.Context, _ environment.Target, b core.CandidateBuild) (task.CandidateContext, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	text, ok := c.files[b.Dockerfile]
	if !ok {
		return task.CandidateContext{}, &core.Failure{Kind: core.FailureInvalidInput, Message: "candidate.dockerfile: there is no file " + b.Dockerfile + " at this commit"}
	}
	sum := sha256.Sum256([]byte(text))
	return task.CandidateContext{Dockerfile: []byte(text), Files: task.Tree{"Dockerfile": {Data: []byte(text)}}, Digest: hex.EncodeToString(sum[:])}, nil
}

const (
	probeProgram = "/probe/vibesys-cli"
	probePause   = 7 * time.Second
	startup      = 3 * time.Minute
	limit        = time.Hour
)

// gpus gives every container of the placement one GPU, and the variable that passes it.
var gpus = &fakeresource.Fake{
	Of: core.ResourceGPU, Facts: map[string]string{"gpu_id": "0"},
	Attached: resource.Attachment{GPUs: resource.GPUs{Devices: "device=0", Runtime: "fake"}, GPUEnv: map[string]string{"FAKE_VISIBLE_DEVICES": "0"}},
	Claimed:  []core.Claim{{Resource: "gpu:0", Amount: 1}},
}

// candidateScene is a scene whose environment builds candidates and probes their servers.
type candidateScene struct {
	*scene
	files  *candidateFiles
	timers *envtest.Timers
}

func newCandidateScene(t *testing.T, more ...resource.Resource) *candidateScene {
	t.Helper()
	files := &candidateFiles{files: map[string]string{"serve/Dockerfile": "FROM busybox:1.36.1\nCOPY . /srv\n"}}
	timers := envtest.NewTimers()
	s := newScene(t, options{timers: timers, configure: func(c *docker.Config) {
		c.Candidates = files
		c.Probe = probeProgram
		c.ProbePause = probePause
		c.Resources = append([]resource.Resource{gpus}, more...)
	}})
	return &candidateScene{scene: s, files: files, timers: timers}
}

var build = core.CandidateBuild{Dockerfile: "serve/Dockerfile", Context: "serve"}

func (s *candidateScene) candidate() core.CandidateImage {
	s.t.Helper()
	img, err := s.env.Candidate(context.Background(), build, nil)
	if err != nil {
		s.t.Fatal(err)
	}
	return img
}

// execution runs command in the task's container beside a server that runs serve.
func (s *candidateScene) execution(serve []string, command ...string) core.Execution {
	return core.Execution{
		Checkout: checkout(s.t), Argv: command, Timeout: limit,
		Candidate: &core.CandidateRun{Image: s.candidate().ID, Serve: &core.CandidateServe{Argv: serve, Ready: "/health", StartupTimeout: startup}},
	}
}

// pump fires every pause between two probes as it is armed, until ctx ends: how long a server
// takes to be ready never decides the result, only whether it ever is.
func (s *candidateScene) pump(ctx context.Context) {
	for n := 1; ; n++ {
		timer := s.timers.Await(ctx, n)
		if timer == nil {
			return
		}
		if timer.Duration == probePause {
			timer.Fire()
		}
	}
}

// start runs x, with the probes pumped, and returns a wait for its attempt and its output.
func (s *candidateScene) start(ctx context.Context, x core.Execution) func() (core.Attempt, string) {
	var (
		out     bytes.Buffer
		attempt core.Attempt
		wg      sync.WaitGroup
	)
	pumping, stop := context.WithCancel(context.Background())
	wg.Go(func() {
		defer stop()
		attempt = s.env.Run(ctx, x, &out)
	})
	wg.Go(func() { s.pump(pumping) })
	return func() (core.Attempt, string) {
		wg.Wait()
		return attempt, out.String()
	}
}

func (s *candidateScene) noContainersLeft() {
	s.t.Helper()
	if left := s.daemon.Containers(); len(left) != 0 {
		s.t.Fatalf("containers left after the run: %+v", left)
	}
}

func (s *candidateScene) reach(point string) {
	s.t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), envtest.Backstop)
	defer cancel()
	if !s.world.Reached(ctx, point) {
		s.t.Fatalf("the run never reached %q", point)
	}
}

func TestACandidatesImageIsBuiltOnceForWhatBuildsItAndNamedByItsDigest(t *testing.T) {
	s := newCandidateScene(t)
	before := s.daemon.Builds()
	first := s.candidate()
	if s.daemon.Builds() != before+1 {
		t.Fatalf("builds: %d, want %d", s.daemon.Builds(), before+1)
	}
	if key, ok := strings.CutPrefix(first.Tag, "vibesys-candidate:task-"); !ok || len(key) != 32 || strings.Trim(key, "0123456789abcdef") != "" {
		t.Fatalf("tag %q, want vibesys-candidate:task-<32 hexadecimal digits>", first.Tag)
	}
	if again := s.candidate(); again != first || s.daemon.Builds() != before+1 {
		t.Fatalf("the same files built again: %+v after %+v, %d builds", again, first, s.daemon.Builds())
	}
	s.files.mu.Lock()
	s.files.files["serve/Dockerfile"] += "RUN true\n"
	s.files.mu.Unlock()
	if changed := s.candidate(); changed.Tag == first.Tag || s.daemon.Builds() != before+2 {
		t.Fatalf("changed files gave %+v after %+v, %d builds", changed, first, s.daemon.Builds())
	}
}

// Regression: the candidate's image was named by its files alone, so a base image rebuilt under
// the same tag left every later candidate of the same files on the old base.
func TestACandidateIsBuiltAgainOnABaseImageRebuiltUnderTheSameTag(t *testing.T) {
	s := newCandidateScene(t)
	const ref = "registry.example/serving/base:dev"
	s.files.mu.Lock()
	s.files.files["serve/Dockerfile"] = "FROM " + ref + "\nCOPY . /srv\n"
	s.files.mu.Unlock()
	s.daemon.Load(ref, "base v1")
	first := s.candidate()
	if again := s.candidate(); again != first {
		t.Fatalf("an unchanged base built again: %+v after %+v", again, first)
	}
	builds := s.daemon.Builds()
	s.daemon.Load(ref, "base v2")
	rebuilt := s.candidate()
	if s.daemon.Builds() != builds+1 || rebuilt.ID == first.ID || rebuilt.Tag == first.Tag {
		t.Fatalf("the base %s changed under its tag, and the candidate is %+v after %+v, with %d builds after %d", ref, rebuilt, first, s.daemon.Builds(), builds)
	}
}

func TestACandidateThatCannotBeReadIsRefusedNamingWhy(t *testing.T) {
	s := newCandidateScene(t)
	_, err := s.env.Candidate(context.Background(), core.CandidateBuild{Dockerfile: "gone/Dockerfile", Context: "gone"}, nil)
	if f := core.AsFailure(err); f.Kind != core.FailureInvalidInput || !strings.Contains(f.Message, "gone/Dockerfile") {
		t.Fatalf("got %v", err)
	}
}

// The server runs in the candidate's image with the GPUs; the command runs in the task's image,
// without them, in the server's network, with the probe and the server's address.
func TestTheServerHasTheGPUsAndTheCommandSharesItsNetwork(t *testing.T) {
	s := newCandidateScene(t)
	x := s.execution([]string{"print", "serving on ${PORT}", ";", "ready", ";", "hold", "forever"}, "mark", "loaded", ";", "hold", "loaded-go", ";", "print", "load done")
	wait := s.start(context.Background(), x)
	s.reach("loaded")

	boxes := s.daemon.Containers()
	if len(boxes) != 2 {
		t.Fatalf("containers %+v, want the server's and the command's", boxes)
	}
	var server, command dockerfake.Info
	for _, b := range boxes {
		if strings.HasSuffix(b.Name, "-candidate") {
			server = b
		} else {
			command = b
		}
	}
	if server.Spec.Image != x.Candidate.Image {
		t.Errorf("the server runs %s, want the candidate's image %s", server.Spec.Image, x.Candidate.Image)
	}
	if server.Spec.GPUs.Devices != "device=0" || server.Spec.Env["FAKE_VISIBLE_DEVICES"] != "0" {
		t.Errorf("the server lacks the GPUs: %+v", server.Spec)
	}
	if command.Spec.GPUs != (docker.GPUs{}) || command.Spec.Env["FAKE_VISIBLE_DEVICES"] != "" {
		t.Errorf("the command's container has GPUs: %+v", command.Spec)
	}
	if command.Spec.Network != "container:"+server.Name {
		t.Errorf("the command's network is %q, want the server's", command.Spec.Network)
	}
	url := command.Spec.Env[core.EnvServiceURL]
	if !strings.HasPrefix(url, "http://127.0.0.1:") {
		t.Errorf("%s = %q", core.EnvServiceURL, url)
	}
	probe := false
	for _, m := range command.Spec.Mounts {
		probe = probe || (m.Host == probeProgram && m.Container == docker.ProbeMount && m.ReadOnly)
	}
	if !probe {
		t.Errorf("the probe is not mounted read-only: %+v", command.Spec.Mounts)
	}

	s.world.Release("loaded-go")
	a, out := wait()
	if e, ok := a.(core.Ended); !ok || e.Exec.Kind != core.ExecExited || e.Exec.ExitCode != 0 {
		t.Fatalf("attempt %+v", a)
	}
	port := strings.TrimPrefix(url, "http://127.0.0.1:")
	if !strings.Contains(out, "load done") || !strings.Contains(out, "serving on "+port) {
		t.Fatalf("output %q lacks the command's or the server's lines on port %s", out, port)
	}
	s.noContainersLeft()
}

// Every way a run of two containers ends removes both.
func TestEveryEndingOfTwoContainersRemovesBoth(t *testing.T) {
	ready := []string{"print", "server up", ";", "ready", ";", "hold", "forever"}
	cases := []struct {
		name    string
		serve   []string
		command []string
		// act ends the run from outside, once it is under way.
		act  func(s *candidateScene, cancel func())
		kind core.ExecKind
		code int
		// log is in the reason's server log, when set.
		log string
	}{
		{name: "the command exits", serve: ready, command: []string{"exit", "0"}, kind: core.ExecExited},
		{name: "the command crashes", serve: ready, command: []string{"print", "x", ";", "exit", "7"}, kind: core.ExecExited, code: 7},
		{
			name: "the server is never ready", serve: []string{"print", "loading weights", ";", "mark", "serving", ";", "hold", "forever"}, command: []string{"exit", "0"},
			act:  func(s *candidateScene, _ func()) { s.reach("serving"); s.fire(startup) },
			kind: core.ExecServiceNotReady, log: "loading weights",
		},
		{
			name: "the server exits early", serve: []string{"print", "out of memory", ";", "exit", "3"}, command: []string{"exit", "0"},
			kind: core.ExecServiceExited, log: "out of memory",
		},
		{
			name: "a cancel during the load", serve: ready, command: []string{"mark", "loading", ";", "hold", "forever"},
			act:  func(s *candidateScene, cancel func()) { s.reach("loading"); cancel() },
			kind: core.ExecCancelled,
		},
		{
			name: "the limit during the load", serve: ready, command: []string{"mark", "loading", ";", "hold", "forever"},
			act:  func(s *candidateScene, _ func()) { s.reach("loading"); s.fire(limit) },
			kind: core.ExecTimedOut,
		},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			s := newCandidateScene(t)
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			wait := s.start(ctx, s.execution(c.serve, c.command...))
			if c.act != nil {
				c.act(s, cancel)
			}
			a, _ := wait()
			e, ok := a.(core.Ended)
			if !ok || e.Exec.Kind != c.kind || (c.kind == core.ExecExited && e.Exec.ExitCode != c.code) {
				t.Fatalf("attempt %+v, want %s %d", a, c.kind, c.code)
			}
			if c.log != "" && !strings.Contains(e.ServiceLog, c.log) {
				t.Fatalf("the server's log %q lacks %q", e.ServiceLog, c.log)
			}
			if running := s.daemon.Running(); len(running) != 0 {
				t.Fatalf("still running: %v", running)
			}
			s.noContainersLeft()
		})
	}
}

// fire fires the first timer armed for d.
func (s *candidateScene) fire(d time.Duration) {
	s.t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), envtest.Backstop)
	defer cancel()
	for n := 1; ; n++ {
		timer := s.timers.Await(ctx, n)
		if timer == nil {
			s.t.Fatalf("no timer of %s was armed", d)
		}
		if timer.Duration == d {
			timer.Fire()
			return
		}
	}
}

// The GPUs go to the server, the mounted directories to both containers read-only, and a cluster
// to the command's container.
func TestEachContainerGetsItsShareOfThePlacementsResources(t *testing.T) {
	mounts := &fakeresource.Fake{
		Of: core.ResourceMounts, Facts: map[string]string{"mounts_id": "m"},
		Attached: resource.Attachment{Mounts: []resource.Mount{{Host: "/models", Container: "/models"}}},
		Claimed:  []core.Claim{{Resource: "mounts:m", Amount: 1}},
	}
	cluster := &fakeresource.Fake{
		Of: core.ResourceKubernetes, Facts: map[string]string{"kubernetes_id": "k"},
		Attached: resource.Attachment{Env: map[string]string{"KUBECONFIG": "/kube/config"}, Mounts: []resource.Mount{{Host: "/kube", Container: "/kube", ReadOnly: true}}},
		Claimed:  []core.Claim{{Resource: "kubernetes:k", Amount: 1}},
	}
	s := newCandidateScene(t, mounts, cluster)
	wait := s.start(context.Background(), s.execution([]string{"ready", ";", "hold", "forever"}, "mark", "shares", ";", "hold", "shares-go"))
	s.reach("shares")
	var server, command dockerfake.Info
	for _, b := range s.daemon.Containers() {
		if strings.HasSuffix(b.Name, "-candidate") {
			server = b
		} else {
			command = b
		}
	}
	mounted := func(c dockerfake.Info, dir string) (bool, bool) {
		for _, m := range c.Spec.Mounts {
			if m.Container == dir {
				return true, m.ReadOnly
			}
		}
		return false, false
	}
	for _, c := range []dockerfake.Info{server, command} {
		if ok, ro := mounted(c, "/models"); !ok || !ro {
			t.Errorf("%s: /models mounted %v, read-only %v; want read-only", c.Name, ok, ro)
		}
	}
	if ok, _ := mounted(server, "/kube"); ok || server.Spec.Env["KUBECONFIG"] != "" {
		t.Errorf("the server got the cluster: %+v", server.Spec)
	}
	if ok, _ := mounted(command, "/kube"); !ok || command.Spec.Env["KUBECONFIG"] != "/kube/config" {
		t.Errorf("the command lacks the cluster: %+v", command.Spec)
	}
	if server.Spec.GPUs.Devices == "" || command.Spec.GPUs.Devices != "" {
		t.Errorf("GPUs: server %+v, command %+v", server.Spec.GPUs, command.Spec.GPUs)
	}
	s.world.Release("shares-go")
	if a, _ := wait(); !isExit0(a) {
		t.Fatalf("attempt %+v", a)
	}
	s.noContainersLeft()
}

func isExit0(a core.Attempt) bool {
	e, ok := a.(core.Ended)
	return ok && e.Exec.Kind == core.ExecExited && e.Exec.ExitCode == 0
}

func TestAPlacementWithoutAProbeRunsNoTaskOfTwoImages(t *testing.T) {
	files := &candidateFiles{files: map[string]string{"serve/Dockerfile": "FROM busybox:1.36.1\n"}}
	s := newScene(t, options{configure: func(c *docker.Config) { c.Candidates = files }})
	img, err := s.env.Candidate(context.Background(), build, nil)
	if err != nil {
		t.Fatal(err)
	}
	a := s.env.Run(context.Background(), core.Execution{
		Checkout: checkout(t), Argv: []string{"exit", "0"}, Timeout: limit,
		Candidate: &core.CandidateRun{Image: img.ID, Serve: &core.CandidateServe{Argv: []string{"ready"}, Ready: "/", StartupTimeout: startup}},
	}, nil)
	if _, ok := a.(core.NotStarted); !ok {
		t.Fatalf("attempt %+v, want one refused before anything starts", a)
	}
	if left := s.daemon.Containers(); len(left) != 0 {
		t.Fatalf("containers %+v", left)
	}
}
