package production

import (
	"context"
	"encoding/json"
	"errors"
	"io/fs"
	"math/rand/v2"
	"os"
	"strings"
	"testing"

	"google.golang.org/protobuf/encoding/protojson"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

// speaks is a job tool that answers `--version --json` with the protocol it speaks, as a build of that
// revision does; protocol 0 is a build that predates the command and takes it for no command, and
// missing is a tool that cannot be run. It answers `daemon ensure --json` with a daemon of that protocol.
type speaks struct {
	protocol uint32
	missing  bool
}

func (s speaks) Run(_ context.Context, program string, args, _ []string) ([]byte, []byte, error) {
	switch {
	case s.missing:
		return nil, nil, &fs.PathError{Op: "fork/exec", Path: program, Err: fs.ErrNotExist}
	case len(args) == 2 && args[0] == "--version":
		if s.protocol == 0 {
			return nil, []byte("usage: vibesys-cli COMMAND\n"), errors.New("exit status 2")
		}
		out, err := protojson.Marshal(&cliv1.BuildVersions{BinaryVersion: "0.1.2-local", ProtocolVersion: s.protocol, MinProtocolVersion: 1})
		return out, nil, err
	}
	out, err := protojson.Marshal(&cliv1.DaemonEnsureResponse{
		Socket: "/run/u/daemon.sock", RuntimeDirectory: "/run/u",
		Versions: &cliv1.BuildVersions{BinaryVersion: "0.1.2-local", ProtocolVersion: s.protocol, MinProtocolVersion: 1},
	})
	return out, nil, err
}

// tooled is Parts whose job tool is the one named, found where the host's configuration says, over a
// runner that says what that tool speaks.
func tooled(runner Runner) Parts {
	return Parts{
		Config: &pb.HostConfig{JobTool: "/home/u/.local/bin/vibesys-cli", SessionHolderCommand: "/bin/holder", SkillsDirectory: "/skills"},
		Find:   developerBuild, Files: everythingRuns{}, Runner: runner,
	}
}

// everythingRuns is a file system on which every path is an executable file.
type everythingRuns struct{}

func (everythingRuns) Executable(string) bool { return true }

// For every revision a tool can speak, the host knows before a run starts whether it can use it: a tool
// below the revision the host was built against is a typed too-old problem of the job tool at readiness,
// is refused when a start asks for its endpoint, and is never asked for a daemon; one at or above is
// ready and gets its daemon. This is the host job tool that read "[evaluator] tasks are not supported by
// this build" in the middle of a run.
func TestAJobToolOlderThanTheHostIsRefusedBeforeARunStarts(t *testing.T) {
	r := rand.New(rand.NewPCG(5, 11))
	protocols := []uint32{0, 1, 2, 3, RequiredJobToolProtocol - 1, RequiredJobToolProtocol, RequiredJobToolProtocol + 1}
	for range 50 {
		protocols = append(protocols, uint32(r.IntN(40)))
	}
	for _, protocol := range protocols {
		parts := tooled(speaks{protocol: protocol})
		var job *pb.PartReadiness
		for _, part := range parts.Readiness(context.Background()) {
			if part.GetPart() == pb.HostPart_HOST_PART_JOB_TOOL {
				job = part
			}
		}
		_, endpointErr := parts.JobToolEndpoint(context.Background())
		var tooOld *JobToolTooOldError
		if protocol < RequiredJobToolProtocol {
			problem := job.GetProblem()
			// The running daemon keeps its too-old tool until it is restarted, so the detail says so,
			// not only which setting picks a newer build.
			if problem.GetReason() != pb.PartProblemReason_PART_PROBLEM_REASON_TOO_OLD || !strings.Contains(problem.GetDetail(), "/home/u/.local/bin/vibesys-cli") || !strings.Contains(problem.GetDetail(), "host restart") {
				t.Errorf("protocol %d: readiness %v, want a too-old job tool named, with host restart advice", protocol, job)
			}
			if !errors.As(endpointErr, &tooOld) {
				t.Errorf("protocol %d: endpoint %v, want a *JobToolTooOldError", protocol, endpointErr)
			}
			continue
		}
		if job.GetReady() == nil || endpointErr != nil {
			t.Errorf("protocol %d: readiness %v, endpoint %v, want both ready", protocol, job, endpointErr)
		}
	}
}

// The revision this host asks of a job tool is the one that added the task schema its release's tasks
// are written in, which the tool's own build is held to by the same file.
func TestTheRequiredJobToolProtocolIsTheConformanceFile(t *testing.T) {
	raw, err := os.ReadFile("../../../proto/vibesys/cli/v1/conformance/protocol.json")
	if err != nil {
		t.Fatal(err)
	}
	var file struct {
		TaskSchema02Since uint32 `json:"task_schema_02_since"`
	}
	if err := json.Unmarshal(raw, &file); err != nil || file.TaskSchema02Since != RequiredJobToolProtocol {
		t.Fatalf("protocol.json says %d (%v), this host requires %d", file.TaskSchema02Since, err, RequiredJobToolProtocol)
	}
}

// A tool that cannot be run is not an old one: it keeps the reason it always had.
func TestAJobToolThatCannotBeRunIsNotCalledOld(t *testing.T) {
	parts := tooled(speaks{missing: true})
	for _, part := range parts.Readiness(context.Background()) {
		if part.GetPart() != pb.HostPart_HOST_PART_JOB_TOOL {
			continue
		}
		reason := part.GetProblem().GetReason()
		if reason == pb.PartProblemReason_PART_PROBLEM_REASON_TOO_OLD || reason == pb.PartProblemReason_PART_PROBLEM_REASON_UNSPECIFIED {
			t.Errorf("job tool %v, want a problem that is not too-old", part)
		}
	}
}
