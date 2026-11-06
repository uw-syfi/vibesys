package wire

import (
	"slices"

	"google.golang.org/protobuf/types/known/durationpb"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/task"
)

// This file carries what a job of a task of two images knows of its candidate: how its image is
// built, and its server.

func candidateBuild(b core.CandidateBuild) *cliv1.CandidateBuild {
	return &cliv1.CandidateBuild{Dockerfile: b.Dockerfile, Context: b.Context, AllowedBases: slices.Clone(b.AllowedBases)}
}

func toCandidateBuild(field string, b *cliv1.CandidateBuild) (core.CandidateBuild, error) {
	if b.GetDockerfile() == "" || b.GetContext() == "" {
		return core.CandidateBuild{}, invalid("%s: the candidate's image has a Dockerfile and a build context", field)
	}
	return core.CandidateBuild{Dockerfile: b.GetDockerfile(), Context: b.GetContext(), AllowedBases: slices.Clone(b.GetAllowedBases())}, nil
}

func candidateServe(s core.CandidateServe) *cliv1.CandidateServe {
	return &cliv1.CandidateServe{Argv: slices.Clone(s.Argv), Ready: s.Ready, StartupTimeout: durationpb.New(s.StartupTimeout)}
}

func toCandidateServe(field string, s *cliv1.CandidateServe) (core.CandidateServe, error) {
	if len(s.GetArgv()) == 0 || s.GetArgv()[0] == "" {
		return core.CandidateServe{}, invalid("%s.argv: the candidate's server needs a program to run", field)
	}
	if err := s.GetStartupTimeout().CheckValid(); err != nil || s.GetStartupTimeout().AsDuration() <= 0 {
		return core.CandidateServe{}, invalid("%s.startup_timeout: the candidate's server has a positive startup time", field)
	}
	return core.CandidateServe{Argv: slices.Clone(s.GetArgv()), Ready: s.GetReady(), StartupTimeout: s.GetStartupTimeout().AsDuration()}, nil
}

// planCandidate is the contract's message for the candidate of a plan.
func planCandidate(c *task.Candidate) *cliv1.CandidateSetup {
	if c == nil {
		return nil
	}
	return &cliv1.CandidateSetup{
		Build: candidateBuild(c.Build),
		Serve: candidateServe(core.CandidateServe{Argv: c.Serve, Ready: c.Ready, StartupTimeout: c.StartupTimeout}),
	}
}

func toPlanCandidate(field string, c *cliv1.CandidateSetup) (*task.Candidate, error) {
	if c == nil {
		return nil, nil
	}
	if c.GetServe() == nil {
		return nil, invalid("%s.serve: a check or a measure job of a task of two images runs the candidate's server", field)
	}
	b, err := toCandidateBuild(field+".build", c.GetBuild())
	if err != nil {
		return nil, err
	}
	s, err := toCandidateServe(field+".serve", c.GetServe())
	if err != nil {
		return nil, err
	}
	return &task.Candidate{Build: b, Serve: s.Argv, Ready: s.Ready, StartupTimeout: s.StartupTimeout}, nil
}

// commandCandidate is the contract's message for a command job's candidate.
func commandCandidate(c *core.CommandCandidate) *cliv1.CandidateSetup {
	if c == nil {
		return nil
	}
	out := &cliv1.CandidateSetup{Build: candidateBuild(c.Build)}
	if c.Serve != nil {
		out.Serve = candidateServe(*c.Serve)
	}
	return out
}

// ToCommandCandidate reads the candidate of a command job, nil when it has none.
func ToCommandCandidate(field string, c *cliv1.CandidateSetup) (*core.CommandCandidate, error) {
	if c == nil {
		return nil, nil
	}
	b, err := toCandidateBuild(field+".build", c.GetBuild())
	if err != nil {
		return nil, err
	}
	out := &core.CommandCandidate{Build: b}
	if c.GetServe() != nil {
		s, err := toCandidateServe(field+".serve", c.GetServe())
		if err != nil {
			return nil, err
		}
		out.Serve = &s
	}
	return out, nil
}

func profileRequest(p *core.ProfileRequest) *cliv1.ProfileRequest {
	if p == nil {
		return nil
	}
	return &cliv1.ProfileRequest{Wrap: slices.Clone(p.Wrap)}
}

// ToProfileRequest reads the profile request of a command job, nil when it is not a profile.
func ToProfileRequest(field string, p *cliv1.ProfileRequest) (*core.ProfileRequest, error) {
	if p == nil {
		return nil, nil
	}
	if slices.Contains(p.GetWrap(), "") {
		return nil, invalid("%s.wrap: a word of the wrapper is empty", field)
	}
	return &core.ProfileRequest{Wrap: slices.Clone(p.GetWrap())}, nil
}
