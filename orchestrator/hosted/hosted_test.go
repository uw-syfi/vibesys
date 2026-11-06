package hosted_test

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/test/bufconn"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/hosted"
	"github.com/uw-syfi/vibesys/orchestrator/release/releasetest"
)

// start opens an orchestrator over a fresh state directory with the given settings.
func start(t *testing.T, settings map[string]string) *hosted.Orchestrator {
	t.Helper()
	// A Unix socket path is short, so the directory is made under the temporary directory's root.
	state, err := os.MkdirTemp("", "h")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(state) })
	settings["VIBESYS_ORCHESTRATOR_STATE_DIR"] = state
	return startAt(t, state, filepath.Join(state, "bin", "vibesys-host"), "test", settings)
}

// startAt opens an orchestrator over a state directory, as a host binary at executable.
func startAt(t *testing.T, state, executable, version string, settings map[string]string) *hosted.Orchestrator {
	t.Helper()
	settings["VIBESYS_ORCHESTRATOR_STATE_DIR"] = state
	ctx, cancel := context.WithCancel(context.Background())
	x := hosted.Start(ctx, hosted.Options{
		Version: version, Executable: executable, Log: slog.New(slog.NewTextHandler(io.Discard, nil)),
		Getenv: func(k string) string { return settings[k] }, Home: state, Stop: cancel,
	})
	t.Cleanup(func() {
		cancel()
		<-x.Done()
	})
	return x
}

// identify asks a host process what it is, over an in-memory connection to the services it
// registers.
func identify(t *testing.T, x *hosted.Orchestrator, client string) *pb.HostIdentity {
	t.Helper()
	conn := serve(t, x)
	id, err := pb.NewHostControlServiceClient(conn).Identify(context.Background(), &pb.IdentifyRequest{ClientRelease: client})
	if err != nil {
		t.Fatal(err)
	}
	return id
}

func serve(t *testing.T, x *hosted.Orchestrator) *grpc.ClientConn {
	t.Helper()
	listener := bufconn.Listen(1 << 20)
	server := grpc.NewServer()
	if err := x.Register(server); err != nil {
		t.Fatal(err)
	}
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(server.Stop)
	conn, err := grpc.NewClient("passthrough:///host", grpc.WithTransportCredentials(insecure.NewCredentials()),
		grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) { return listener.DialContext(ctx) }))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = conn.Close() })
	return conn
}

func part(id *pb.HostIdentity, which pb.HostPart) *pb.PartReadiness {
	for _, p := range id.GetParts() {
		if p.GetPart() == which {
			return p
		}
	}
	return nil
}

// A CLI setting that names no executable does not stop the orchestrator from serving: the host says,
// in a typed answer, that its job tool is refused and which setting is wrong, and no run can start
// with a build nobody chose.
func TestACLISettingThatNamesNoExecutableIsReportedAsAJobToolProblem(t *testing.T) {
	for _, setting := range []string{"vibesys-cli", "/missing/vibesys-cli"} {
		x := start(t, map[string]string{"VIBESYS_ORCHESTRATOR_CLI": setting})
		if x.Reason() != "" {
			t.Fatalf("setting %q: the orchestrator should still be served, got %q", setting, x.Reason())
		}
		problem := part(identify(t, x, "test"), pb.HostPart_HOST_PART_JOB_TOOL).GetProblem()
		if problem.GetReason() != pb.PartProblemReason_PART_PROBLEM_REASON_OPERATOR_CHOICE_REFUSED || !strings.Contains(problem.GetDetail(), "VIBESYS_ORCHESTRATOR_CLI") {
			t.Errorf("setting %q: problem %v, want the setting named", setting, problem)
		}
	}
}

// With no setting, no release and none on its PATH the host has no job tool to give the agents: the
// skills have no PATH fallback, so it reports that, and a run is not started.
func TestNoCLIToGiveTheAgentsIsAProblemOfTheJobTool(t *testing.T) {
	x := start(t, map[string]string{"PATH": "/nonexistent"})
	problem := part(identify(t, x, "test"), pb.HostPart_HOST_PART_JOB_TOOL).GetProblem()
	if problem.GetReason() != pb.PartProblemReason_PART_PROBLEM_REASON_NO_RELEASE || !strings.Contains(problem.GetDetail(), "VIBESYS_ORCHESTRATOR_CLI") {
		t.Fatalf("problem %v, want the setting named and no release", problem)
	}
}

func TestAnExecutableCLISettingIsAJobToolTheOperatorChose(t *testing.T) {
	cli := filepath.Join(t.TempDir(), "vibesys-cli")
	if err := os.WriteFile(cli, []byte("#!/bin/sh\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	x := start(t, map[string]string{"VIBESYS_ORCHESTRATOR_CLI": cli})
	if x.Reason() != "" {
		t.Fatalf("not served: %s", x.Reason())
	}
	if ready := part(identify(t, x, "test"), pb.HostPart_HOST_PART_JOB_TOOL).GetReady(); ready.GetOrigin() != pb.PartOrigin_PART_ORIGIN_OPERATOR {
		t.Fatalf("ready = %v, want the operator's choice", ready)
	}
}

// A release with a part missing is reported, and repairing the install is seen by the same running
// host with no restart: the refusal is not remembered for the life of the process.
func TestARepairedInstallIsSeenByARunningHost(t *testing.T) {
	dir := t.TempDir()
	releasetest.Write(t, dir, releasetest.Without("bin/vibesys-cli"),
		releasetest.Content("marketplace/.claude-plugin/marketplace.json", `{"name": "vibesys", "plugins": [{"name": "vibesys", "source": "./skills"}]}`))
	state, err := os.MkdirTemp("", "h")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(state) })
	x := startAt(t, state, filepath.Join(dir, "bin", "vibesys-host"), "1.0.0", map[string]string{})
	problem := part(identify(t, x, "1.0.0"), pb.HostPart_HOST_PART_JOB_TOOL).GetProblem()
	if problem.GetReason() != pb.PartProblemReason_PART_PROBLEM_REASON_RELEASE_INCOMPLETE || !strings.Contains(problem.GetDetail(), "bin/vibesys-cli") {
		t.Fatalf("problem %v, want the release's missing tool named", problem)
	}
	if err := os.WriteFile(filepath.Join(dir, "bin", "vibesys-cli"), []byte("#!/bin/sh\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	id := identify(t, x, "1.0.0")
	if ready := part(id, pb.HostPart_HOST_PART_JOB_TOOL).GetReady(); ready.GetOrigin() != pb.PartOrigin_PART_ORIGIN_RELEASE {
		t.Fatalf("after the repair: %v", part(id, pb.HostPart_HOST_PART_JOB_TOOL))
	}
	for _, p := range id.GetParts() {
		if p.GetProblem() != nil {
			t.Errorf("part %v: %v", p.GetPart(), p.GetProblem())
		}
	}
	if id.GetRelease() != "1.0.0" || id.GetRelation() != pb.ReleaseRelation_RELEASE_RELATION_SAME || id.GetServing() == nil {
		t.Fatalf("identity %v", id)
	}
}

// A host that cannot serve the orchestrator still answers HostControlService, and says, as data,
// why and which release holds it.
func TestAHostThatCannotServeTheOrchestratorStillIdentifiesItself(t *testing.T) {
	state, err := os.MkdirTemp("", "h")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(state) })
	first := startAt(t, state, "/x/bin/vibesys-host", "1.0.0", map[string]string{})
	if first.Reason() != "" {
		t.Skipf("the first host does not serve here: %s", first.Reason())
	}
	second := startAt(t, state, "/y/bin/vibesys-host", "1.0.1", map[string]string{})
	id := identify(t, second, "1.0.0")
	unserved := id.GetUnserved()
	if unserved.GetReason() != pb.UnservedReason_UNSERVED_REASON_HELD_ELSEWHERE || unserved.GetHeldByRelease() != "1.0.0" {
		t.Fatalf("unserved %v, want held elsewhere by release 1.0.0", unserved)
	}
	if id.GetRelease() != "1.0.1" || id.GetRelation() != pb.ReleaseRelation_RELEASE_RELATION_HOST_NEWER || id.GetServing() != nil {
		t.Fatalf("identity %v", id)
	}
}

// A state directory the host refuses is reported as such.
func TestARefusedStateDirectoryIsReportedAsData(t *testing.T) {
	refused := hosted.Start(context.Background(), hosted.Options{
		Version: "dev", Executable: "/x/bin/vibesys-host", Log: slog.New(slog.NewTextHandler(io.Discard, nil)),
		Getenv: func(k string) string {
			if k == "VIBESYS_ORCHESTRATOR_STATE_DIR" {
				return "relative/state"
			}
			return ""
		}, Home: "/nonexistent", Stop: func() {},
	})
	id := identify(t, refused, "")
	if id.GetUnserved().GetReason() != pb.UnservedReason_UNSERVED_REASON_STATE_REFUSED || id.GetRelation() != pb.ReleaseRelation_RELEASE_RELATION_UNKNOWN {
		t.Fatalf("identity %v", id)
	}
}

func TestHostProgramGolden(t *testing.T) {
	data, err := os.ReadFile("../../proto/testdata/host_program.golden.json")
	if err != nil {
		t.Fatal(err)
	}
	var golden struct{ Program string }
	if err := json.Unmarshal(data, &golden); err != nil {
		t.Fatal(err)
	}
	if hosted.HostProgram != golden.Program {
		t.Fatalf("program = %q, want %q", hosted.HostProgram, golden.Program)
	}
	command := filepath.Join("../../gateway/cmd", golden.Program)
	if info, err := os.Stat(command); err != nil || !info.IsDir() {
		t.Fatalf("command directory: %v", err)
	}
	build, err := os.ReadFile("../../gateway/build.sh")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(build), "./cmd/"+golden.Program) || !strings.Contains(string(build), "/"+golden.Program+`"`) {
		t.Fatalf("build.sh does not build %s", golden.Program)
	}
}
