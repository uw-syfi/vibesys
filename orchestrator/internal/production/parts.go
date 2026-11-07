package production

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"time"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/hostconfig"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/release"
)

// jobToolBound limits a start's wait for the job tool's daemon: a daemon it has to bring up first
// recovers its store, and the tool's own limit on all of it is longer than a client should wait.
const jobToolBound = 30 * time.Second

// jobToolSpeaksBound limits the question of which versions a job tool speaks: the tool answers it
// from its own binary, with no daemon, so it is a process start and no more.
const jobToolSpeaksBound = 10 * time.Second

// RequiredJobToolProtocol is the revision of vibesys-cli's contract this host needs of the job tool: the one
// that reads the task schema the release's tasks are written in (TaskSchema02Since). It is pinned
// against protocol.json, which the tool's own build is held to as well, so a tool built before it is
// refused by name before a run starts and not by a task it cannot read once an agent has begun.
const RequiredJobToolProtocol uint32 = 5

// JobToolTooOldError is a job tool that runs but does not speak the revision this host needs.
type JobToolTooOldError struct {
	Tool string
	// Speaks is the protocol the tool says it speaks, or 0 when it does not say (a build that
	// predates `--version --json`).
	Speaks uint32
	// Build is the identity the tool reports, empty when it reports none.
	Build string
}

func (e *JobToolTooOldError) Error() string {
	what := "does not say which protocol it speaks"
	if e.Speaks != 0 {
		what = fmt.Sprintf("speaks protocol %d", e.Speaks)
	}
	build := ""
	if e.Build != "" {
		build = " (build " + e.Build + ")"
	}
	return fmt.Sprintf("the job tool %s%s is older than this host needs: it %s, and this host needs %d. Install the vibesys-cli of this release, or choose a newer one with `vibesys-client host configure --job-tool PATH`",
		e.Tool, build, what, RequiredJobToolProtocol)
}

// Parts resolves what a run is started with: the program that holds an agent session, the skills a
// session loads, and the tool the skills run. Each is resolved when it is asked for, on every
// launch and every start check, as a few stats, so repairing an install takes effect with no
// restart and a refusal is never remembered past the problem that caused it.
type Parts struct {
	// Config is the host's configuration, which chooses any part in place of the release's.
	Config *pb.HostConfig
	// Find looks for the release the host is installed in.
	Find  release.Finder
	Files Files
	// Home is the home directory of the host's default tool path.
	Home string
	// Runner runs the job tool.
	Runner Runner
}

// SessionHolder is the command that holds a session, and who chose it.
func (p Parts) SessionHolder() ([]string, pb.PartOrigin, error) {
	command, err := sidecarCommand(p.Config.GetSessionHolderCommand(), p.Find)
	return command, origin(p.Config.GetSessionHolderCommand()), err
}

// Skills is the plugin every session loads, and who chose it.
func (p Parts) Skills() (Plugin, pb.PartOrigin, error) {
	plugin, err := PluginFor(p.Config.GetSkillsDirectory(), p.Find)
	return plugin, origin(p.Config.GetSkillsDirectory()), err
}

// JobTool is the tool the skills run, and who chose it.
func (p Parts) JobTool() (string, pb.PartOrigin, error) {
	tool, err := ResolveCLI(p.Config.GetJobTool(), hostconfig.ToolPath(p.Config, p.Home), p.Find, p.Files)
	return tool, origin(p.Config.GetJobTool()), err
}

// JobToolSpeaks asks the tool which revision of its contract it speaks and refuses one older than this
// host needs, with a *JobToolTooOldError. It starts no daemon, so it is safe to ask of any tool at any
// time. A tool that cannot be run is that error of the operating system, not an old tool.
func (p Parts) JobToolSpeaks(ctx context.Context, tool string) error {
	ctx, cancel := context.WithTimeout(ctx, jobToolSpeaksBound)
	defer cancel()
	speaks, err := askWhatToolSpeaks(ctx, p.Runner, tool)
	if err != nil {
		return err
	}
	if speaks.Protocol < RequiredJobToolProtocol {
		return &JobToolTooOldError{Tool: tool, Speaks: speaks.Protocol, Build: speaks.Build}
	}
	return nil
}

// JobToolEndpoint is what the agents of a run that starts now use: the job tool as it is resolved now,
// the revision it speaks (a tool too old for this host's tasks is refused here, before any daemon is
// started for it and before any agent spends tokens), and the daemon it answers with, which it has
// made sure of. So a start reaches a daemon the tool could use, or is refused saying why, and the host
// holds no rule for where that daemon is.
func (p Parts) JobToolEndpoint(ctx context.Context) (core.JobToolEndpoint, error) {
	tool, _, err := p.JobTool()
	if err != nil {
		return core.JobToolEndpoint{}, err
	}
	if err := p.JobToolSpeaks(ctx, tool); err != nil {
		return core.JobToolEndpoint{}, err
	}
	ctx, cancel := context.WithTimeout(ctx, jobToolBound)
	defer cancel()
	answer, err := ExecEnsurerFor(core.JobToolEndpoint{Executable: tool}, p.Runner).Answer(ctx)
	if err != nil {
		return core.JobToolEndpoint{}, fmt.Errorf("the job tool %s has no daemon for this run: %w", tool, err)
	}
	return core.JobToolEndpoint{Executable: tool, RuntimeDirectory: answer.GetRuntimeDirectory()}, nil
}

func origin(setting string) pb.PartOrigin {
	if strings.TrimSpace(setting) != "" {
		return pb.PartOrigin_PART_ORIGIN_OPERATOR
	}
	return pb.PartOrigin_PART_ORIGIN_RELEASE
}

// Check is nil when every part can be found now, and otherwise says what is wrong with each part that
// cannot. It looks at files only; what the job tool speaks is asked by JobToolEndpoint when a start
// is dispatched, and by Readiness.
func (p Parts) Check() error {
	var problems []string
	for _, r := range p.resolved() {
		if r.err != nil {
			problems = append(problems, r.err.Error())
		}
	}
	if len(problems) == 0 {
		return nil
	}
	return errors.New(strings.Join(problems, "; "))
}

// resolution is one part as the files say it is.
type resolution struct {
	part   pb.HostPart
	origin pb.PartOrigin
	err    error
}

func (p Parts) resolved() []resolution {
	_, so, serr := p.SessionHolder()
	_, ko, kerr := p.Skills()
	_, jo, jerr := p.JobTool()
	return []resolution{
		{pb.HostPart_HOST_PART_SESSION_HOLDER, so, serr},
		{pb.HostPart_HOST_PART_SKILLS, ko, kerr},
		{pb.HostPart_HOST_PART_JOB_TOOL, jo, jerr},
	}
}

// Readiness is every part as it is at this moment, the one answer for everything a run needs of the
// host: each part is found, and the job tool also says which revision it speaks, so a mismatch is a
// typed problem of that part, as a missing file is.
func (p Parts) Readiness(ctx context.Context) []*pb.PartReadiness {
	var out []*pb.PartReadiness
	for _, r := range p.resolved() {
		err := r.err
		if err == nil && r.part == pb.HostPart_HOST_PART_JOB_TOOL {
			tool, _, _ := p.JobTool()
			err = p.JobToolSpeaks(ctx, tool)
		}
		ready := &pb.PartReadiness{Part: r.part}
		if err != nil {
			ready.State = &pb.PartReadiness_Problem{Problem: &pb.PartProblem{Reason: problemReason(r.origin, err), Detail: err.Error()}}
		} else {
			ready.State = &pb.PartReadiness_Ready{Ready: &pb.PartReady{Origin: r.origin}}
		}
		out = append(out, ready)
	}
	return out
}

func problemReason(origin pb.PartOrigin, err error) pb.PartProblemReason {
	var notInstalled *release.NotInstalledError
	var tooOld *JobToolTooOldError
	switch {
	case errors.As(err, &tooOld):
		return pb.PartProblemReason_PART_PROBLEM_REASON_TOO_OLD
	case origin == pb.PartOrigin_PART_ORIGIN_OPERATOR:
		return pb.PartProblemReason_PART_PROBLEM_REASON_OPERATOR_CHOICE_REFUSED
	case errors.As(err, &notInstalled):
		return pb.PartProblemReason_PART_PROBLEM_REASON_NO_RELEASE
	}
	return pb.PartProblemReason_PART_PROBLEM_REASON_RELEASE_INCOMPLETE
}
