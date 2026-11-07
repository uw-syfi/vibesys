package docker

import (
	"context"
	"crypto/sha256"
	"encoding/binary"
	"fmt"
	"io"
	"regexp"
	"strings"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
	"github.com/uw-syfi/vibesys/cli/internal/task"
)

// A task of two images builds a second image from the candidate's own commit, its candidate
// image, and runs a run's commands in two containers: the candidate's server in a container of the
// candidate's image, with the placement's GPUs, and the command in a container of the task's image,
// with none, joined to the first one's network so that the server is at 127.0.0.1.

// Where the task container finds what probes the candidate's server, and how a probe is run.
const (
	// ProbeMount is where the probe program is mounted, read-only, in the task's container.
	ProbeMount = "/vibesys/bin/vibesys-cli"
	// ProbeCommand is the probe program's command that asks a URL once: it exits 0 for a 2xx answer.
	ProbeCommand = "probe-ready"
	// candidateRepository names the candidate images.
	candidateRepository = "vibesys-candidate"
	// candidateSuffix ends the name of a run's candidate container.
	candidateSuffix = "-candidate"
)

// CandidateSource reads what builds a candidate's image, from the repository at the target's
// commit: never from a working tree.
type CandidateSource interface {
	Candidate(ctx context.Context, target environment.Target, build core.CandidateBuild) (task.CandidateContext, error)
}

// CandidateFiles is the CandidateSource that reads the files with a task.Files, as the daemon
// reads tasks.
type CandidateFiles struct{ Files task.Files }

var _ CandidateSource = CandidateFiles{}

// Candidate implements CandidateSource.
func (c CandidateFiles) Candidate(ctx context.Context, target environment.Target, b core.CandidateBuild) (task.CandidateContext, error) {
	tree, err := c.Files.Files(ctx, target.Repository, target.Commit, task.CandidatePaths(b))
	if err != nil {
		return task.CandidateContext{}, err
	}
	out, err := task.ReadCandidate(tree, b)
	if err != nil {
		f := core.AsFailure(err)
		f.Message = fmt.Sprintf("commit %s: %s", target.Commit, f.Message)
		return task.CandidateContext{}, &f
	}
	return out, nil
}

var tagUnsafe = regexp.MustCompile(`[^a-z0-9_.-]+`)

// candidateTag names a candidate's image by the task and its build key (buildKey).
func candidateTag(taskName, key string) string {
	name := strings.Trim(tagUnsafe.ReplaceAllString(strings.ToLower(taskName), "-"), ".-")
	if name == "" {
		name = "task"
	}
	if len(name) > 64 {
		name = name[:64]
	}
	return candidateRepository + ":" + name + "-" + key[:32]
}

// Candidate implements environment.Environment: the candidate's image, built from the target's
// commit when the engine has no image of the same build key: the digest of the candidate's files,
// and the engine's local image of each base its Dockerfile builds on. A build that fails says why, with the
// end of its output.
func (e *env) Candidate(ctx context.Context, b core.CandidateBuild, log io.Writer) (core.CandidateImage, error) {
	p := e.p
	if p.cfg.Candidates == nil {
		return core.CandidateImage{}, &core.Failure{Kind: core.FailureUnavailable, Message: "docker: this environment was given no source of candidates' files"}
	}
	if log == nil {
		log = io.Discard
	}
	if _, err := e.spec(ctx); err != nil {
		return core.CandidateImage{}, err
	}
	files, err := p.cfg.Candidates.Candidate(ctx, e.target, b)
	if err != nil {
		return core.CandidateImage{}, err
	}
	buildContext, err := Context(Spec{Dockerfile: files.Dockerfile, Files: files.Files})
	if err != nil {
		return core.CandidateImage{}, err
	}
	select {
	case p.build <- struct{}{}:
		defer func() { <-p.build }()
	case <-ctx.Done():
		return core.CandidateImage{}, ctx.Err()
	}
	bases, err := p.bases(ctx, files.Dockerfile)
	if err != nil {
		return core.CandidateImage{}, err
	}
	tag := candidateTag(e.target.Task, buildKey([]byte(files.Digest), bases))
	img, ok, err := p.cfg.Engine.Image(ctx, tag)
	if err == nil && !ok {
		_, _ = fmt.Fprintf(log, "[candidate] building %s from %s\n", tag, b.Dockerfile)
		if err = p.cfg.Engine.Build(ctx, BuildSpec{Tag: tag, Context: buildContext, Labels: Labels{LabelOwner: Owner}, Role: CandidateImage}, log); err == nil {
			img, ok, err = p.cfg.Engine.Image(ctx, tag)
			if err == nil && !ok {
				err = &core.Failure{Kind: core.FailureInternal, Message: fmt.Sprintf("docker: the engine built %s and does not have it", tag)}
			}
		}
	}
	if img, err = checked(img, err); err != nil {
		return core.CandidateImage{}, err
	}
	_, _ = fmt.Fprintf(log, "[candidate] %s is %s\n", tag, img.ID)
	return core.CandidateImage{ID: img.ID, Tag: tag}, nil
}

// PortOf is a port taken from a seed, such as a run's id, for an engine whose containers have a
// network of their own, so that any port is free in them and no two runs assume one fixed port.
func PortOf(runID string) int {
	sum := sha256.Sum256([]byte(runID))
	return 20000 + int(binary.BigEndian.Uint32(sum[:4])%20000)
}
