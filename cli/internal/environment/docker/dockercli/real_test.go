//go:build docker && unix

package dockercli_test

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"os"
	"strings"
	"sync/atomic"
	"syscall"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/backend"
	"github.com/uw-syfi/vibesys/cli/internal/backend/cpu"
	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker/dockercli"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker/dockertest"
	"github.com/uw-syfi/vibesys/cli/internal/environment/envtest"
)

// These tests drive a real Docker daemon, so they run only where there is one: go test -tags docker.
// They are what holds the fake engine to the real one: both pass the engine contract. Every
// container they make carries a label only this run uses, and they remove only those.

// baseImage is pinned by digest, so a test's image is the same on every machine.
const baseImage = "busybox:1.36.1@sha256:73aaf090f3d85aa34ee199857f03fa3a95c8ede2ffd4cc2cdb5b94e566b11662"

const dockerfile = "FROM " + baseImage + "\nRUN true\n"

// scopeVar passes the scope of this run of the tests to the helper processes the suite kills.
const scopeVar = "DOCKERCLI_TEST_SCOPE"

var facts = core.MachineFacts{HostID: "host-a", OS: "linux", Arch: "amd64", CPUModel: "Test CPU", LogicalCPUs: 8, MemoryBytes: 16 << 30}

func cpuBackend() backend.Backend {
	return cpu.New(cpu.NewFakeSystem(facts), clock.NewManual(time.Unix(1, 0), time.Millisecond))
}

func randomHex(n int) string {
	b := make([]byte, n)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

func TestMain(m *testing.M) {
	if envtest.HelperMain(func() environment.Environment { return newEnvironment(clock.System{}, os.Getenv(scopeVar), nil) }) {
		return
	}
	// The scope is this run's own, and the helpers it starts inherit it.
	os.Setenv(scopeVar, "t"+randomHex(6))
	code := m.Run()
	removeScope(os.Getenv(scopeVar))
	os.Exit(code)
}

func newEngine() *dockercli.Engine {
	e, err := dockercli.New(dockercli.Config{Timers: clock.System{}})
	if err != nil {
		panic(err)
	}
	return e
}

// removeScope removes the containers of the scope, and nothing else.
func removeScope(scope string) {
	ctx, cancel := context.WithTimeout(context.Background(), time.Minute)
	defer cancel()
	e := newEngine()
	names, err := e.Containers(ctx, docker.Labels{docker.LabelScope: scope})
	if err != nil {
		fmt.Fprintln(os.Stderr, "listing the containers of the test scope:", err)
		return
	}
	for _, name := range names {
		_ = e.Remove(ctx, name)
	}
}

// source serves one Dockerfile for every target.
type source struct{ spec docker.Spec }

func (s source) Spec(context.Context, environment.Target) (docker.Spec, error) { return s.spec, nil }

// countingEngine counts builds, to show an image that exists is not built again.
type countingEngine struct {
	docker.Engine
	builds atomic.Int32
}

func (c *countingEngine) Build(ctx context.Context, spec docker.BuildSpec, log io.Writer) error {
	c.builds.Add(1)
	return c.Engine.Build(ctx, spec, log)
}

func newProvider(timers clock.Timers, scope string, engine docker.Engine, dockerfile string) *docker.Provider {
	if engine == nil {
		engine = newEngine()
	}
	p, err := docker.New(docker.Config{
		Engine: engine, Source: source{docker.Spec{Dockerfile: []byte(dockerfile)}}, Backend: cpuBackend(),
		Timers: timers, IDs: dockercli.RandomIDs{}, Scope: scope,
		User: docker.User{UID: os.Getuid(), GID: os.Getgid()},
	})
	if err != nil {
		panic(err)
	}
	return p
}

var target = environment.Target{Repository: "/repo", Commit: "0123456789abcdef0123456789abcdef01234567", Task: "task"}

func newEnvironment(timers clock.Timers, scope string, engine docker.Engine) environment.Environment {
	env, err := newProvider(timers, scope, engine, dockerfile).Environment(target)
	if err != nil {
		panic(err)
	}
	return env
}

func TestRealEngineMeetsTheEngineContract(t *testing.T) {
	dockertest.Run(t, dockertest.Rig{
		World:      func(t *testing.T) envtest.World { return envtest.NewShellWorld(t) },
		Dir:        func(_ *testing.T, w envtest.World) string { return w.(*envtest.ShellWorld).Dir() },
		Engine:     func(*testing.T, envtest.World) docker.Engine { return newEngine() },
		Dockerfile: []byte(dockerfile),
	})
}

// The docker environment over the real engine is held to the same contract as the host.
func TestDockerOverTheRealEnginePassesTheEnvironmentContract(t *testing.T) {
	scope := os.Getenv(scopeVar)
	t.Cleanup(func() { removeScope(scope) })
	envtest.Run(t, envtest.Rig{
		New: func(t *testing.T, _ envtest.World, timers clock.Timers) environment.Environment {
			return newEnvironment(timers, scope, nil)
		},
		World: func(t *testing.T) envtest.World { return envtest.NewShellWorld(t) },
		// The world's directory is the checkout, so the container mounts it and reaches the
		// pipes the programs signal through.
		Checkout: func(_ *testing.T, w envtest.World) core.Checkout {
			return core.Checkout{Commit: target.Commit, Dir: w.(*envtest.ShellWorld).Dir()}
		},
		Spawn: func(t *testing.T, _ envtest.World, x core.Execution) func() {
			return envtest.SpawnProcess(t, x)
		},
	})
}

func run(t *testing.T, env environment.Environment, dir string, argv ...string) (core.ExecOutcome, string) {
	t.Helper()
	var out strings.Builder
	ctx, cancel := context.WithTimeout(context.Background(), envtest.Backstop)
	defer cancel()
	res, err := env.Run(ctx, core.Execution{
		Checkout: core.Checkout{Commit: target.Commit, Dir: dir}, Argv: argv, Timeout: envtest.Backstop,
	}, &syncWriter{b: &out})
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	return res, out.String()
}

func TestAnUnchangedTaskIsNotRebuiltAndKeepsItsDigest(t *testing.T) {
	scope := "t" + randomHex(6)
	t.Cleanup(func() { removeScope(scope) })
	engine := &countingEngine{Engine: newEngine()}
	p := newProvider(clock.System{}, scope, engine, dockerfile)
	first, err := p.Environment(target)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), envtest.Backstop)
	defer cancel()
	fp1, err := first.Fingerprint(ctx)
	if err != nil {
		t.Fatalf("Fingerprint: %v", err)
	}
	if fp1.ImageDigest == "" {
		t.Fatalf("fingerprint %+v has no image digest", fp1)
	}
	// The image may be there from an earlier run of the tests; what counts is that it is built at
	// most once, and not again for the same task.
	builtOnce := engine.builds.Load()
	if builtOnce > 1 {
		t.Fatalf("the first Fingerprint built the image %d times", builtOnce)
	}
	again, _ := p.Environment(target)
	fp2, err := again.Fingerprint(ctx)
	if err != nil {
		t.Fatalf("second Fingerprint: %v", err)
	}
	if res, _ := run(t, again, t.TempDir(), "true"); res.Kind != core.ExecExited || res.ExitCode != 0 {
		t.Fatalf("outcome %+v", res)
	}
	if n := engine.builds.Load(); n != builtOnce {
		t.Errorf("the image was built again: %d builds after the first Fingerprint, %d at the end", builtOnce, n)
	}
	if fp1.ImageDigest != fp2.ImageDigest {
		t.Errorf("the digest changed from %s to %s without a change to the task", fp1.ImageDigest, fp2.ImageDigest)
	}
}

func TestCommandsRunAsTheInvokingUser(t *testing.T) {
	scope := "t" + randomHex(6)
	t.Cleanup(func() { removeScope(scope) })
	env, err := newProvider(clock.System{}, scope, nil, dockerfile).Environment(target)
	if err != nil {
		t.Fatal(err)
	}
	dir := t.TempDir()
	if real, err := evalSymlinks(dir); err == nil {
		dir = real
	}
	res, out := run(t, env, dir, "sh", "-c", "id -u && id -g && touch made-by-the-command")
	if res.Kind != core.ExecExited || res.ExitCode != 0 {
		t.Fatalf("outcome %+v, output %q", res, out)
	}
	if want := fmt.Sprintf("%d\n%d\n", os.Getuid(), os.Getgid()); out != want {
		t.Errorf("output %q, want %q", out, want)
	}
	info, err := os.Stat(dir + "/made-by-the-command")
	if err != nil {
		t.Fatal(err)
	}
	if uid := info.Sys().(*syscall.Stat_t).Uid; int(uid) != os.Getuid() {
		t.Errorf("the file belongs to user %d, want the invoking user %d", uid, os.Getuid())
	}
}

func TestACommandWritesToTheScratchDirectoryAndTheHostReadsIt(t *testing.T) {
	scope := "t" + randomHex(6)
	t.Cleanup(func() { removeScope(scope) })
	env, err := newProvider(clock.System{}, scope, nil, dockerfile).Environment(target)
	if err != nil {
		t.Fatal(err)
	}
	dir, scratch := t.TempDir(), t.TempDir()
	for _, p := range []*string{&dir, &scratch} {
		if real, err := evalSymlinks(*p); err == nil {
			*p = real
		}
	}
	var out strings.Builder
	ctx, cancel := context.WithTimeout(context.Background(), envtest.Backstop)
	defer cancel()
	res, err := env.Run(ctx, core.Execution{
		Checkout: core.Checkout{Commit: target.Commit, Dir: dir}, Scratch: scratch,
		Argv: []string{"sh", "-c", `printf '{"ops": 1}' > "$1/sample-1.out"`, "sh", scratch}, Timeout: envtest.Backstop,
	}, &syncWriter{b: &out})
	if err != nil || res.Kind != core.ExecExited || res.ExitCode != 0 {
		t.Fatalf("Run = %+v, %v, output %q", res, err, out.String())
	}
	data, err := os.ReadFile(scratch + "/sample-1.out")
	if err != nil || string(data) != `{"ops": 1}` {
		t.Fatalf("the host reads %q, %v from the scratch directory", data, err)
	}
	info, err := os.Stat(scratch + "/sample-1.out")
	if err != nil {
		t.Fatal(err)
	}
	if uid := info.Sys().(*syscall.Stat_t).Uid; int(uid) != os.Getuid() {
		t.Errorf("the file belongs to user %d, want the invoking user %d", uid, os.Getuid())
	}
}

func TestAFinishedRunLeavesNoContainer(t *testing.T) {
	scope := "t" + randomHex(6)
	t.Cleanup(func() { removeScope(scope) })
	env, err := newProvider(clock.System{}, scope, nil, dockerfile).Environment(target)
	if err != nil {
		t.Fatal(err)
	}
	run(t, env, t.TempDir(), "true")
	ctx, cancel := context.WithTimeout(context.Background(), envtest.Backstop)
	defer cancel()
	left, err := newEngine().Containers(ctx, docker.Labels{docker.LabelScope: scope})
	if err != nil || len(left) != 0 {
		t.Fatalf("containers %v after the run (%v)", left, err)
	}
}

func TestALeftoverContainerIsRemovedByTheNextRun(t *testing.T) {
	scope := "t" + randomHex(6)
	t.Cleanup(func() { removeScope(scope) })
	engine := newEngine()
	ctx, cancel := context.WithTimeout(context.Background(), envtest.Backstop)
	defer cancel()
	// Make the image, then a container of this scope that no run owns, as a killed supervisor
	// leaves one.
	p := newProvider(clock.System{}, scope, engine, dockerfile)
	env, err := p.Environment(target)
	if err != nil {
		t.Fatal(err)
	}
	fp, err := env.Fingerprint(ctx)
	if err != nil {
		t.Fatal(err)
	}
	dir := t.TempDir()
	leftover := docker.ContainerSpec{
		Name: "vibesys-cli-" + randomHex(8), Image: fp.ImageDigest,
		Labels: docker.Labels{docker.LabelOwner: docker.Owner, docker.LabelScope: scope, docker.LabelRun: randomHex(8)},
		User:   docker.User{UID: os.Getuid(), GID: os.Getgid()}, Dir: dir, Mounts: []docker.Mount{{Host: dir, Container: dir}},
	}
	if _, err := engine.Start(ctx, leftover); err != nil {
		t.Fatalf("Start of the leftover: %v", err)
	}
	if res, _ := run(t, env, dir, "true"); res.Kind != core.ExecExited {
		t.Fatalf("outcome %+v", res)
	}
	left, err := engine.Containers(ctx, docker.Labels{docker.LabelScope: scope})
	if err != nil || len(left) != 0 {
		t.Fatalf("containers %v after the next run (%v), want the leftover gone", left, err)
	}
}

func TestABuildThatFailsSaysWhy(t *testing.T) {
	engine := newEngine()
	ctx, cancel := context.WithTimeout(context.Background(), envtest.Backstop)
	defer cancel()
	marker := "marker-" + randomHex(6)
	err := engine.Build(ctx, docker.BuildSpec{
		Tag:        "vibesys-cli-contract-fail:" + randomHex(6),
		Dockerfile: []byte("FROM " + baseImage + "\nRUN echo " + marker + " && exit 3\n"),
	}, discard{})
	var f *core.Failure
	if !errors.As(err, &f) {
		t.Fatalf("Build of a failing Dockerfile: %v, want a failure", err)
	}
	if !strings.Contains(f.Message, marker) {
		t.Errorf("the failure %q does not carry the end of the build log", f.Message)
	}
}

func TestMissingDockerIsUnavailableAndSaysSo(t *testing.T) {
	engine, err := dockercli.New(dockercli.Config{Program: "/no/such/docker", Timers: clock.System{}})
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), envtest.Backstop)
	defer cancel()
	_, _, err = engine.Image(ctx, "busybox")
	assertUnavailable(t, err, "docker")
}

func TestAnUnreachableDaemonIsUnavailableAndSaysSo(t *testing.T) {
	t.Setenv("DOCKER_HOST", "unix:///no/such/docker.sock")
	ctx, cancel := context.WithTimeout(context.Background(), envtest.Backstop)
	defer cancel()
	_, _, err := newEngine().Image(ctx, "busybox")
	assertUnavailable(t, err, "docker")
}

func assertUnavailable(t *testing.T, err error, mention string) {
	t.Helper()
	var f *core.Failure
	if !errors.As(err, &f) || f.Kind != core.FailureUnavailable {
		t.Fatalf("error %v, want an unavailable failure", err)
	}
	if !strings.Contains(strings.ToLower(f.Message), mention) {
		t.Errorf("message %q does not say what is unavailable", f.Message)
	}
}

func TestAnImageWithoutAShellIsRefusedAtTheRun(t *testing.T) {
	scope := "t" + randomHex(6)
	t.Cleanup(func() { removeScope(scope) })
	p := newProvider(clock.System{}, scope, nil, "FROM scratch\nLABEL vibesys-cli-test=no-shell\n")
	env, err := p.Environment(target)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), envtest.Backstop)
	defer cancel()
	_, err = env.Run(ctx, core.Execution{
		Checkout: core.Checkout{Commit: target.Commit, Dir: t.TempDir()}, Argv: []string{"true"}, Timeout: envtest.Backstop,
	}, discard{})
	if err == nil || !strings.Contains(err.Error(), "no /bin/sh") {
		t.Fatalf("Run in an image without a shell: %v, want it to say the image has no /bin/sh", err)
	}
	left, err := newEngine().Containers(ctx, docker.Labels{docker.LabelScope: scope})
	if err != nil || len(left) != 0 {
		t.Errorf("containers %v after the refused run (%v)", left, err)
	}
}
