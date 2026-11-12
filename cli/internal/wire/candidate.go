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

// CommandImage is the contract's value for the image of a command job.
func CommandImage(i core.CommandImage) cliv1.CommandImage {
	switch i {
	case core.ImageHarness:
		return cliv1.CommandImage_COMMAND_IMAGE_HARNESS
	case core.ImageCandidate:
		return cliv1.CommandImage_COMMAND_IMAGE_CANDIDATE
	}
	return cliv1.CommandImage_COMMAND_IMAGE_UNSPECIFIED
}

// ToCommandImage reads the image of a command job. Unspecified reads as core.ImageUnspecified,
// which only a job stored before the image was named holds; the daemon refuses a submission of it.
func ToCommandImage(field string, i cliv1.CommandImage) (core.CommandImage, error) {
	switch i {
	case cliv1.CommandImage_COMMAND_IMAGE_UNSPECIFIED:
		return core.ImageUnspecified, nil
	case cliv1.CommandImage_COMMAND_IMAGE_HARNESS:
		return core.ImageHarness, nil
	case cliv1.CommandImage_COMMAND_IMAGE_CANDIDATE:
		return core.ImageCandidate, nil
	}
	return "", invalid("%s: %d is not an image (harness or candidate)", field, int32(i))
}
