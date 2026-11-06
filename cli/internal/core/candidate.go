package core

import (
	"strings"
	"time"
)

// A task of two images (task schema 0.2) runs its judge and its load in the task's image and the
// candidate's server in an image the candidate's own commit builds. The types below are how that
// second image is built, how its server is started, and what was built.

// ProjectRootPlaceholder is how a task's command spells the root of the checkout it runs in.
const ProjectRootPlaceholder = "${PROJECT_ROOT}"

// ResolveProjectRoot is the argv with ProjectRootPlaceholder replaced by the checkout's root.
func ResolveProjectRoot(argv []string, root string) []string {
	out := make([]string, len(argv))
	for i, a := range argv {
		out[i] = strings.ReplaceAll(a, ProjectRootPlaceholder, root)
	}
	return out
}

// PortPlaceholder is how the candidate's server command spells the port it listens on, which the
// environment chooses for each execution: machines are shared, so no port is fixed.
const PortPlaceholder = "${PORT}"

// EnvServiceURL names the variable a command of the task's image reaches the candidate's server
// at: http://127.0.0.1:<port>.
const EnvServiceURL = "VIBESYS_SERVICE_URL"

// CandidateBuild is how a candidate's image is built from its commit.
type CandidateBuild struct {
	// Dockerfile is the path of the Dockerfile in the checkout of the commit.
	Dockerfile string
	// Context is the build context, a directory of the checkout. A .dockerignore in it is honored.
	Context string
	// AllowedBases are the exact references the Dockerfile's first FROM may name. Empty allows any.
	AllowedBases []string
}

// CandidateImage is a candidate image that was built, or found already built.
type CandidateImage struct {
	// ID is the image's exact digest, sha256: and 64 hexadecimal digits: what a result records.
	ID string
	// Tag names the image by the task and the digest of what built it.
	Tag string
	// Content is the digest of what a container of the image runs (docker.Engine.Content), the
	// same for two images of one content whatever their IDs, or empty when the environment cannot
	// say it.
	Content string
}

// SameAs reports whether a container of the image runs what a container of o runs: they are one
// image, or two of one known content.
func (i CandidateImage) SameAs(o CandidateImage) bool {
	return i.ID == o.ID || (i.Content != "" && i.Content == o.Content)
}

// ValidImageDigest reports whether s is an exact image digest.
func ValidImageDigest(s string) bool { return imageDigest.MatchString(s) }

// CandidateServe is the candidate's server as an execution starts it.
type CandidateServe struct {
	// Argv is the server's command, with the checkout's root resolved and PortPlaceholder not.
	Argv []string
	// Ready is the HTTP path that answers 2xx once the server is ready.
	Ready string
	// StartupTimeout bounds the wait for the server to be ready.
	StartupTimeout time.Duration
}

// CandidateRun says how an execution uses a candidate's image.
type CandidateRun struct {
	// Image is the exact digest of the candidate's image.
	Image string
	// Serve, when set, starts the server in a container of Image, and runs the command in a
	// container of the task's image beside it, sharing its network, once the server is ready.
	// Nil runs the command, and its service if it has one, in a container of Image.
	Serve *CandidateServe
}
