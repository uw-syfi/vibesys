package docker

import (
	"context"
	"crypto/sha256"
	"errors"
	"fmt"
	"io"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/lease"
)

// BuildPoll is how long a process that finds another one building an image of the same lease
// (buildLease) waits before it tries to take the lease again.
const BuildPoll = 5 * time.Second

// buildLeases is how many leases the builds of every image share: an image's builds take the
// lease its tag falls in. They are few, so the directory of a machine's leases holds a bounded
// number of files however many images are built there; two images whose tags fall in one lease
// are built one after the other.
const buildLeases = 256

// buildLease names the lease the builds of the tag take.
func buildLease(tag string) lease.Key {
	sum := sha256.Sum256([]byte(tag))
	return lease.Key(fmt.Sprintf("vibesys-image-build-%02x", int(sum[0])%buildLeases))
}

// ensure returns the engine's image of the tag, built from the build context when the engine has
// none. announce, when not empty, goes to log before a build.
//
// Every process that builds on the engine with the same Builds builds one tag one at a time: a
// build holds the tag's lease there, and one that finds it held says so to log, waits for it, and
// then uses the image the holder built, or builds it itself when the holder did not.
//
// A build that fails while the engine then has an image under the tag lost a race with a builder
// that takes no lease at the same place, such as one of a sandbox daemon, or of a machine that
// shares the engine without its leases: under the containerd image store the build that ends
// second fails with "already exists". The image is of the same key, since the tag is the key's,
// so it is the image asked for, and is returned. So is the image of a build whose reply was lost.
func (p *Provider) ensure(ctx context.Context, tag string, buildContext []byte, announce string, log io.Writer) (Image, error) {
	if img, ok, err := p.cfg.Engine.Image(ctx, tag); err != nil || ok {
		return checked(img, err)
	}
	held, err := p.buildTurn(ctx, tag, log)
	if err != nil {
		return Image{}, err
	}
	// Given back with the caller's context: one that has ended gives the lease back at once.
	defer func() { _ = held.Release(ctx) }()
	// The holder this process waited for may have built it.
	if img, ok, err := p.cfg.Engine.Image(ctx, tag); err != nil || ok {
		return checked(img, err)
	}
	if announce != "" {
		_, _ = fmt.Fprint(log, announce)
	}
	buildErr := p.cfg.Engine.Build(ctx, BuildSpec{Tag: tag, Context: buildContext, Labels: Labels{LabelOwner: Owner}}, log)
	if buildErr != nil && ctx.Err() != nil {
		return Image{}, buildErr
	}
	img, ok, err := p.cfg.Engine.Image(ctx, tag)
	switch {
	case buildErr != nil && (err != nil || !ok):
		return Image{}, buildErr
	case buildErr != nil:
		_, _ = fmt.Fprintf(log, "[docker] the build of %s failed, and the engine has an image under the tag, which another "+
			"build of the same files and bases made: using it (the build said: %s)\n", tag, core.AsFailure(buildErr).Message)
	case err == nil && !ok:
		err = &core.Failure{Kind: core.FailureInternal, Message: fmt.Sprintf("docker: the engine built %s and does not have it", tag)}
	}
	return checked(img, err)
}

// buildTurn takes the lease of the tag's builds, trying again after each BuildPoll while another
// process holds it. The wait is bounded by ctx, as a build of this process would be: the holder's
// build is bounded by its own caller, and its lease is given back when it ends, however it ends.
func (p *Provider) buildTurn(ctx context.Context, tag string, log io.Writer) (lease.Lease, error) {
	key := buildLease(tag)
	told := false
	for {
		held, err := p.cfg.Builds.Acquire(ctx, key, p.cfg.Holder)
		var busy *lease.HeldError
		if !errors.As(err, &busy) {
			if err != nil {
				if ctx.Err() != nil {
					return nil, ctx.Err()
				}
				f := core.AsFailure(err)
				return nil, &core.Failure{Kind: f.Kind, Message: fmt.Sprintf("docker: taking the lease %s on %s to build %s: %s", key, p.cfg.Builds.Where(), tag, f.Message)}
			}
			return held, nil
		}
		if !told {
			told = true
			_, _ = fmt.Fprintf(log, "[docker] %s builds an image of the lease %s on %s; waiting for it to end before building %s, "+
				"which it may be building\n", busy.Holder, key, busy.Where, tag)
		}
		pause, stop := p.cfg.Timers.Timeout(ctx, BuildPoll)
		select {
		case <-pause:
			stop()
		case <-ctx.Done():
			stop()
			return nil, ctx.Err()
		}
	}
}
