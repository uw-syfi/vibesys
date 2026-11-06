package app

import (
	"context"
	"sync"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
)

// imageContents remembers what the placements' engines said of the image IDs that results of
// earlier versions carry in their fingerprints: the content digest of each image still there, and
// "" for one that is not.
//
// A docker fingerprint pins the content of the task's image (core.Fingerprint.ImageDigest).
// Earlier versions pinned the image's ID, which under Docker's containerd image store differs
// from one build of one content to the next, so a result of theirs is compared by the content of
// the image of its ID, which the engine of the placement it ran on says while it still has the
// image. A digest is never rewritten to anything but the content of the image it names, so two
// different contents never compare as one. One the engine does not have stays as it is and
// compares equal to no content digest: those results stay incomparable, and compare says so with
// the two environments.
//
// What the engine says is remembered for the daemon's life, since the content of the image of an
// ID never changes. A failure to ask is not remembered: the result stays as it is for this call.
type imageContents struct {
	mu    sync.Mutex
	known map[placedImage]string
}

// placedImage is an image digest as a fingerprint recorded it, on the placement its result ran on.
type placedImage struct{ placement, digest string }

// withImageContents returns the results with each docker fingerprint's image ID that its
// placement's engine still has replaced by the content digest of that image. Fingerprints that
// already pin content resolve to nothing, as no image has a content digest for its ID, and stay as
// they are. The results passed are not changed.
func (a *App) withImageContents(ctx context.Context, results []core.Result) []core.Result {
	out := make([]core.Result, len(results))
	for i, r := range results {
		if content, ok := a.imageContent(ctx, r.Environment); ok {
			r.Environment.ImageDigest = content
		}
		out[i] = r
	}
	return out
}

// imageContent is the content of the image of the fingerprint's digest, and false when the
// digest is not of a docker image its placement's engine has.
func (a *App) imageContent(ctx context.Context, f core.Fingerprint) (string, bool) {
	if f.Kind != core.EnvironmentDocker || f.ImageDigest == "" || f.Placement() == "" {
		return "", false
	}
	at := placedImage{placement: f.Placement(), digest: f.ImageDigest}
	a.images.mu.Lock()
	content, asked := a.images.known[at]
	a.images.mu.Unlock()
	if asked {
		return content, content != ""
	}
	env, err := a.cfg.Environments.Environment(core.EnvironmentDocker, environment.Target{Placement: at.placement})
	if err != nil {
		// A placement the operator config no longer has: nothing can say what its image was.
		return "", false
	}
	content, found, err := env.ImageContent(ctx, at.digest)
	if err != nil {
		a.cfg.Logf("reading the content of image %s on placement %s, to compare a result recorded with it: %v", at.digest, at.placement, err)
		return "", false
	}
	if !found {
		content = ""
	}
	a.images.mu.Lock()
	if a.images.known == nil {
		a.images.known = map[placedImage]string{}
	}
	a.images.known[at] = content
	a.images.mu.Unlock()
	return content, content != ""
}
