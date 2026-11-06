package production

import (
	"errors"
	"strings"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/release"
)

// Parts resolves what a run is started with: the program that holds an agent session, the skills a
// session loads, and the tool the skills run. Each is resolved when it is asked for, on every
// launch and every start check, as a few stats, so repairing an install takes effect with no
// restart and a refusal is never remembered past the problem that caused it.
type Parts struct {
	Getenv func(string) string
	// Find looks for the release the host is installed in.
	Find  release.Finder
	Files Files
	// Home is the home directory the job tool's places are resolved from.
	Home string
}

// SessionHolder is the command that holds a session, and who chose it.
func (p Parts) SessionHolder() ([]string, pb.PartOrigin, error) {
	command, err := sidecarCommand(p.Getenv, p.Find)
	return command, p.origin(EnvSidecar), err
}

// Skills is the plugin every session loads, and who chose it.
func (p Parts) Skills() (Plugin, pb.PartOrigin, error) {
	plugin, err := PluginFor(p.Getenv, p.Find)
	return plugin, p.origin(EnvMarketplace), err
}

// JobTool is the tool the skills run, and who chose it.
func (p Parts) JobTool() (string, pb.PartOrigin, error) {
	tool, err := ResolveCLI(p.Getenv, p.Find, p.Files)
	return tool, p.origin(EnvCLI), err
}

// Location is where the agents of a run that starts now find the job tool and its places. It is the
// zero value when the job tool cannot be resolved, which Check refuses before a run starts.
func (p Parts) Location() core.CliLocation {
	tool, _, err := p.JobTool()
	if err != nil {
		return core.CliLocation{}
	}
	return LocationFor(p.Getenv, p.Home, tool)
}

func (p Parts) origin(setting string) pb.PartOrigin {
	if strings.TrimSpace(p.Getenv(setting)) != "" {
		return pb.PartOrigin_PART_ORIGIN_OPERATOR
	}
	return pb.PartOrigin_PART_ORIGIN_RELEASE
}

// Check is nil when every part can be used now, and otherwise says what is wrong with each part
// that cannot.
func (p Parts) Check() error {
	var problems []string
	for _, r := range p.Readiness() {
		if problem := r.GetProblem(); problem != nil {
			problems = append(problems, problem.GetDetail())
		}
	}
	if len(problems) == 0 {
		return nil
	}
	return errors.New(strings.Join(problems, "; "))
}

// Readiness is every part as it is at this moment.
func (p Parts) Readiness() []*pb.PartReadiness {
	var out []*pb.PartReadiness
	add := func(part pb.HostPart, origin pb.PartOrigin, err error) {
		r := &pb.PartReadiness{Part: part}
		if err != nil {
			r.State = &pb.PartReadiness_Problem{Problem: &pb.PartProblem{Reason: problemReason(origin, err), Detail: err.Error()}}
		} else {
			r.State = &pb.PartReadiness_Ready{Ready: &pb.PartReady{Origin: origin}}
		}
		out = append(out, r)
	}
	_, o, err := p.SessionHolder()
	add(pb.HostPart_HOST_PART_SESSION_HOLDER, o, err)
	_, o, err = p.Skills()
	add(pb.HostPart_HOST_PART_SKILLS, o, err)
	_, o, err = p.JobTool()
	add(pb.HostPart_HOST_PART_JOB_TOOL, o, err)
	return out
}

func problemReason(origin pb.PartOrigin, err error) pb.PartProblemReason {
	var notInstalled *release.NotInstalledError
	switch {
	case origin == pb.PartOrigin_PART_ORIGIN_OPERATOR:
		return pb.PartProblemReason_PART_PROBLEM_REASON_OPERATOR_CHOICE_REFUSED
	case errors.As(err, &notInstalled):
		return pb.PartProblemReason_PART_PROBLEM_REASON_NO_RELEASE
	}
	return pb.PartProblemReason_PART_PROBLEM_REASON_RELEASE_INCOMPLETE
}
