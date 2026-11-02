// Package dockertest is the contract suite every docker.Engine passes: the real engine over the
// docker program and the fake the docker environment's simulations run against are held to the
// same behavior, so a behavior a test relies on in the fake is one the real engine has.
//
// The suite does not write commands for any one engine. It describes what a command does as an
// envtest.Program and asks the rig's World to render it, so an engine that runs shell commands
// in real containers and a fake that interprets scripts each receive what they understand. Waits
// follow the events the processes produce; none polls, and every wait has a bound that only
// guards against a hang.
//
// A case that creates containers removes them when it ends, by a label of its own, and touches no
// other container: the engine under test may be a real one that other work shares.
package dockertest

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"io"
	"slices"
	"strings"
	"sync"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker"
	"github.com/uw-syfi/vibesys/cli/internal/environment/envtest"
)

// labelCase marks every container a case makes, with a value of its own.
const labelCase = "io.vibesys.cli.contract-case"

// imageTag names the image every case builds. It is the same for every case and every run, and a
// build carries no labels, so building it again yields the image that is already there and a
// machine that runs the suite often keeps one image of it, not one for each case.
const imageTag = "vibesys-cli-contract:suite"

// Rig is what the suite needs to test one engine.
type Rig struct {
	// Engine returns the engine under test, connected for the case. Containers it starts stop
	// when the case ends, whatever the case did.
	Engine func(t *testing.T, w envtest.World) docker.Engine

	// World returns the world the programs run in, fresh for each case.
	World func(t *testing.T) envtest.World

	// Dir returns the directory the containers work in and mount, fresh for each case. The
	// world's programs can reach everything they need from it.
	Dir func(t *testing.T, w envtest.World) string

	// Dockerfile is a Dockerfile the engine can build.
	Dockerfile []byte
}

// Run runs the suite.
func Run(t *testing.T, rig Rig) {
	t.Helper()
	cases := []struct {
		name string
		fn   func(h *harness)
	}{
		{"an image that is not there is not an error", caseNoImage},
		{"a built image is found by its tag and by its ID", caseBuildAndFind},
		{"a cancelled context fails the call with its error", caseCancelled},
		{"a started container is listed by its labels", caseListed},
		{"a command runs in the working directory with its output in order", caseOutput},
		{"a command reports its exit code, 128 plus the signal, and 127 when it does not exist", caseExitCodes},
		{"killing a process ends it with an unknown outcome, not a code", caseKill},
		{"removing a container ends everything in it", caseRemoveEndsProcesses},
		{"a command whose container was removed is lost, not exited", caseLost},
		{"closing a container stops its processes and leaves it to be removed", caseClose},
		{"removing a container that is not there is not an error", caseRemoveMissing},
		{"a second container of the same name is refused and the first goes on", caseNameInUse},
		{"a spec that cannot be right creates nothing", caseInvalidSpec},
		{"an image that is not there starts nothing", caseUnknownImage},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			h := newHarness(t, rig)
			c.fn(h)
		})
	}
}

type harness struct {
	t          *testing.T
	ctx        context.Context
	cancel     context.CancelFunc
	world      envtest.World
	engine     docker.Engine
	dir        string
	id         string
	image      string // the digest of the image the case's containers are made from
	n          int
	dockerfile []byte

	mu      sync.Mutex
	started []docker.Container
}

func randomID() string {
	var b [8]byte
	_, _ = rand.Read(b[:])
	return hex.EncodeToString(b[:])
}

func newHarness(t *testing.T, rig Rig) *harness {
	t.Helper()
	h := &harness{t: t, id: randomID(), dockerfile: rig.Dockerfile}
	h.ctx, h.cancel = context.WithTimeout(context.Background(), envtest.Backstop)
	h.world = rig.World(t)
	h.dir = rig.Dir(t, h.world)
	h.engine = rig.Engine(t, h.world)
	t.Cleanup(func() {
		// Everything the case made goes, by the label only this case used.
		ctx, cancel := context.WithTimeout(context.Background(), envtest.Backstop)
		defer cancel()
		h.mu.Lock()
		started := h.started
		h.mu.Unlock()
		for _, c := range started {
			c.Close()
		}
		names, err := h.engine.Containers(ctx, docker.Labels{labelCase: h.id})
		if err != nil {
			t.Errorf("listing the case's containers: %v", err)
		}
		for _, name := range names {
			if err := h.engine.Remove(ctx, name); err != nil {
				t.Errorf("removing %s: %v", name, err)
			}
		}
		h.cancel()
	})
	return h
}

// spec returns a spec for a new container of the case's image.
func (h *harness) spec() docker.ContainerSpec {
	h.n++
	return docker.ContainerSpec{
		Name:   "vs-contract-" + h.id + "-" + string(rune('a'+h.n)),
		Image:  h.ensureImage(),
		Labels: docker.Labels{labelCase: h.id},
		User:   docker.User{UID: userID(), GID: groupID()},
		Dir:    h.dir,
		Mounts: []docker.Mount{{Host: h.dir, Container: h.dir}},
	}
}

func (h *harness) ensureImage() string {
	if h.image != "" {
		return h.image
	}
	if err := h.engine.Build(h.ctx, docker.BuildSpec{Tag: imageTag, Dockerfile: h.dockerfile}, io.Discard); err != nil {
		h.t.Fatalf("Build: %v", err)
	}
	img, ok, err := h.engine.Image(h.ctx, imageTag)
	if err != nil || !ok {
		h.t.Fatalf("Image(%s) after Build: %v, %v", imageTag, ok, err)
	}
	h.image = img.ID
	return h.image
}

func (h *harness) start() (docker.Container, docker.ContainerSpec) {
	h.t.Helper()
	spec := h.spec()
	c, err := h.engine.Start(h.ctx, spec)
	if err != nil {
		h.t.Fatalf("Start: %v", err)
	}
	h.mu.Lock()
	h.started = append(h.started, c)
	h.mu.Unlock()
	return c, spec
}

// buffer is an output writer that is safe to read while a process writes.
type buffer struct {
	mu  sync.Mutex
	buf strings.Builder
}

func (b *buffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.Write(p)
}

func (b *buffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.String()
}

// runToEnd runs the program in the container and returns how it ended and what it wrote.
func (h *harness) runToEnd(c docker.Container, p envtest.Program) (environment.Exit, string) {
	h.t.Helper()
	return h.runArgv(c, h.world.Argv(p))
}

func (h *harness) runArgv(c docker.Container, argv []string) (environment.Exit, string) {
	h.t.Helper()
	var out buffer
	proc, err := c.Start(argv, &out)
	if err != nil {
		h.t.Fatalf("Start of %v: %v", argv, err)
	}
	select {
	case exit := <-proc.Exited():
		return exit, out.String()
	case <-h.ctx.Done():
		h.t.Fatalf("the process %v did not end", argv)
		return environment.Exit{}, ""
	}
}

func (h *harness) names(match docker.Labels) []string {
	h.t.Helper()
	names, err := h.engine.Containers(h.ctx, match)
	if err != nil {
		h.t.Fatalf("Containers: %v", err)
	}
	return names
}

func (h *harness) gone(names ...string) {
	h.t.Helper()
	for _, name := range names {
		if !h.world.Gone(h.ctx, name) {
			h.t.Fatalf("the process %q still runs", name)
		}
	}
}

func (h *harness) reached(point string) {
	h.t.Helper()
	if !h.world.Reached(h.ctx, point) {
		h.t.Fatalf("the program never reached %q", point)
	}
}

// tree is a program with a process in the background and a main process, both observable, that
// wait for ever.
func tree() envtest.Program {
	return envtest.Program{
		envtest.Alive("main"), envtest.Child(envtest.Alive("child"), envtest.Hold(envtest.Forever)),
		envtest.Mark("up"), envtest.Hold(envtest.Forever),
	}
}

func failureKind(err error) (core.FailureKind, bool) {
	var f *core.Failure
	if errors.As(err, &f) {
		return f.Kind, true
	}
	return "", false
}

func caseNoImage(h *harness) {
	_, ok, err := h.engine.Image(h.ctx, "vibesys-cli-contract-missing:"+h.id)
	if err != nil || ok {
		h.t.Fatalf("Image of an image that is not there: %v, %v; want not found and no error", ok, err)
	}
}

func caseBuildAndFind(h *harness) {
	if err := h.engine.Build(h.ctx, docker.BuildSpec{Tag: imageTag, Dockerfile: h.dockerfile}, io.Discard); err != nil {
		h.t.Fatalf("Build: %v", err)
	}
	byTag, ok, err := h.engine.Image(h.ctx, imageTag)
	if err != nil || !ok {
		h.t.Fatalf("Image by tag: %v, %v", ok, err)
	}
	if len(byTag.ID) != len("sha256:")+64 || !strings.HasPrefix(byTag.ID, "sha256:") || byTag.Platform == "" {
		h.t.Fatalf("image %+v has no exact digest or no platform", byTag)
	}
	byID, ok, err := h.engine.Image(h.ctx, byTag.ID)
	if err != nil || !ok || byID != byTag {
		h.t.Fatalf("Image by ID: %+v, %v, %v; want %+v", byID, ok, err, byTag)
	}
	// Building again is allowed, and the tag still names an image.
	if err := h.engine.Build(h.ctx, docker.BuildSpec{Tag: imageTag, Dockerfile: h.dockerfile}, io.Discard); err != nil {
		h.t.Fatalf("second Build: %v", err)
	}
	if _, ok, err := h.engine.Image(h.ctx, imageTag); err != nil || !ok {
		h.t.Fatalf("Image after a second Build: %v, %v", ok, err)
	}
}

func caseCancelled(h *harness) {
	ctx, cancel := context.WithCancel(h.ctx)
	cancel()
	if _, _, err := h.engine.Image(ctx, "vibesys-cli-contract-missing:"+h.id); !errors.Is(err, context.Canceled) {
		h.t.Errorf("Image with a cancelled context: %v, want context.Canceled", err)
	}
	if _, err := h.engine.Containers(ctx, docker.Labels{labelCase: h.id}); !errors.Is(err, context.Canceled) {
		h.t.Errorf("Containers with a cancelled context: %v, want context.Canceled", err)
	}
	if err := h.engine.Remove(ctx, "vs-contract-"+h.id); !errors.Is(err, context.Canceled) {
		h.t.Errorf("Remove with a cancelled context: %v, want context.Canceled", err)
	}
	spec := h.spec()
	if _, err := h.engine.Start(ctx, spec); !errors.Is(err, context.Canceled) {
		h.t.Errorf("Start with a cancelled context: %v, want context.Canceled", err)
	}
	if names := h.names(docker.Labels{labelCase: h.id}); len(names) != 0 {
		h.t.Errorf("a cancelled Start left containers %v", names)
	}
}

func caseListed(h *harness) {
	_, spec := h.start()
	if got := h.names(docker.Labels{labelCase: h.id}); !slices.Equal(got, []string{spec.Name}) {
		h.t.Fatalf("Containers by the case's label: %v, want [%s]", got, spec.Name)
	}
	if got := h.names(docker.Labels{labelCase: h.id + "-other"}); len(got) != 0 {
		h.t.Errorf("Containers by another value of the label: %v, want none", got)
	}
	if got := h.names(docker.Labels{labelCase: h.id, "io.vibesys.cli.no-such-label": "x"}); len(got) != 0 {
		h.t.Errorf("Containers need every label to match: got %v with one that no container has", got)
	}
}

func caseOutput(h *harness) {
	c, _ := h.start()
	exit, out := h.runToEnd(c, envtest.Program{envtest.Print("one"), envtest.Warn("two"), envtest.Print("three"), envtest.Warn("four"), envtest.Dir()})
	if exit.Code != 0 || exit.Kind != "" || exit.Err != nil {
		h.t.Fatalf("exit %+v, want code 0", exit)
	}
	lines := strings.Split(strings.TrimSpace(out), "\n")
	if len(lines) != 5 || strings.Join(lines[:4], ",") != "one,two,three,four" {
		h.t.Fatalf("output %q, want the four lines in order and then the directory", out)
	}
	if !samePath(lines[4], h.dir) {
		h.t.Errorf("working directory %q, want %q", lines[4], h.dir)
	}
}

func caseExitCodes(h *harness) {
	c, _ := h.start()
	for _, code := range []int{0, 1, 3, 126, 255} {
		if exit, _ := h.runToEnd(c, envtest.Program{envtest.Exit(code)}); exit.Code != code || exit.Kind != "" {
			h.t.Errorf("exit %+v, want code %d", exit, code)
		}
	}
	if exit, _ := h.runToEnd(c, envtest.Program{envtest.Crash()}); exit.Code != 137 || exit.Kind != "" {
		h.t.Errorf("a killed command: exit %+v, want 137 while its container runs", exit)
	}
	if exit, _ := h.runArgv(c, h.world.Missing()); exit.Code != 127 || exit.Kind != "" {
		h.t.Errorf("a command that does not exist: exit %+v, want 127", exit)
	}
}

func caseKill(h *harness) {
	c, _ := h.start()
	proc, err := c.Start(h.world.Argv(tree()), io.Discard)
	if err != nil {
		h.t.Fatal(err)
	}
	h.reached("up")
	proc.Kill()
	select {
	case exit := <-proc.Exited():
		if exit.Kind != core.ExecUnknown || exit.Code != 0 || exit.Err != nil {
			h.t.Errorf("a killed process ended %+v, want the unknown kind: the engine cannot say what became of its command", exit)
		}
	case <-h.ctx.Done():
		h.t.Fatal("a killed process never reported its end")
	}
}

func caseRemoveEndsProcesses(h *harness) {
	c, spec := h.start()
	var out buffer
	proc, err := c.Start(h.world.Argv(tree()), &out)
	if err != nil {
		h.t.Fatal(err)
	}
	h.reached("up")
	h.reached(envtest.AliveMark("child"))
	if err := h.engine.Remove(h.ctx, spec.Name); err != nil {
		h.t.Fatalf("Remove: %v", err)
	}
	h.gone("main", "child")
	select {
	case <-proc.Exited():
	case <-h.ctx.Done():
		h.t.Fatal("the process did not report its end after its container was removed")
	}
	if got := h.names(docker.Labels{labelCase: h.id}); len(got) != 0 {
		h.t.Errorf("containers %v after Remove", got)
	}
}

func caseLost(h *harness) {
	c, spec := h.start()
	proc, err := c.Start(h.world.Argv(tree()), io.Discard)
	if err != nil {
		h.t.Fatal(err)
	}
	h.reached("up")
	if err := h.engine.Remove(h.ctx, spec.Name); err != nil {
		h.t.Fatalf("Remove: %v", err)
	}
	for _, p := range []interface {
		Exited() <-chan environment.Exit
	}{proc, h.afterRemoval(c)} {
		select {
		case exit := <-p.Exited():
			if exit.Kind != core.ExecUnknown {
				h.t.Errorf("a command of a removed container ended %+v, want the unknown kind: the engine cannot say how it ended", exit)
			}
		case <-h.ctx.Done():
			h.t.Fatal("a command of a removed container never ended")
		}
	}
}

// afterRemoval starts a command in a container that is gone.
func (h *harness) afterRemoval(c docker.Container) environment.Process {
	p, err := c.Start(h.world.Argv(envtest.Program{envtest.Print("x")}), io.Discard)
	if err != nil {
		h.t.Fatalf("Start in a removed container: %v; want a process that reports the loss", err)
	}
	return p
}

func caseClose(h *harness) {
	c, spec := h.start()
	if _, err := c.Start(h.world.Argv(tree()), io.Discard); err != nil {
		h.t.Fatal(err)
	}
	h.reached("up")
	h.reached(envtest.AliveMark("child"))
	c.Close()
	h.gone("main", "child")
	if got := h.names(docker.Labels{labelCase: h.id}); !slices.Equal(got, []string{spec.Name}) {
		h.t.Errorf("containers %v after Close, want [%s] stopped and still there", got, spec.Name)
	}
	if err := h.engine.Remove(h.ctx, spec.Name); err != nil {
		h.t.Errorf("Remove after Close: %v", err)
	}
	c.Close() // closing twice is the same as once
}

func caseRemoveMissing(h *harness) {
	if err := h.engine.Remove(h.ctx, "vs-contract-"+h.id+"-missing"); err != nil {
		h.t.Fatalf("Remove of a container that is not there: %v", err)
	}
}

func caseNameInUse(h *harness) {
	c, spec := h.start()
	_, err := h.engine.Start(h.ctx, spec)
	if kind, ok := failureKind(err); !ok || kind != core.FailureConflict {
		h.t.Fatalf("a second Start of %s: %v, want a conflict", spec.Name, err)
	}
	if exit, out := h.runToEnd(c, envtest.Program{envtest.Print("still here")}); exit.Code != 0 || strings.TrimSpace(out) != "still here" {
		h.t.Fatalf("the first container after the refusal: %+v %q", exit, out)
	}
}

func caseInvalidSpec(h *harness) {
	spec := h.spec()
	spec.Mounts[0].Host = h.dir + ",readonly"
	_, err := h.engine.Start(h.ctx, spec)
	if kind, ok := failureKind(err); !ok || kind != core.FailureInvalidInput {
		h.t.Fatalf("Start with a mount path that ends a field: %v, want invalid input", err)
	}
	if got := h.names(docker.Labels{labelCase: h.id}); len(got) != 0 {
		h.t.Errorf("a refused spec created %v", got)
	}
}

func caseUnknownImage(h *harness) {
	spec := h.spec()
	spec.Image = "sha256:" + strings.Repeat("0", 64)
	if _, err := h.engine.Start(h.ctx, spec); err == nil {
		h.t.Fatal("Start of an image that is not there succeeded")
	}
	if got := h.names(docker.Labels{labelCase: h.id}); len(got) != 0 {
		h.t.Errorf("a failed Start left %v; the caller removes what a failed Start may leave, but an image that is not there leaves nothing", got)
	}
}
