//go:build docker && unix

package dockercli_test

import (
	"archive/tar"
	"bytes"
	"context"
	"errors"
	"io"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker"
	"github.com/uw-syfi/vibesys/cli/internal/environment/envtest"
)

// An image is saved as a docker archive, which carries its manifest; an image that is not there is
// a failure, not an empty archive.
func TestAnImageIsSavedAsADockerArchive(t *testing.T) {
	engine := newEngine()
	ctx, cancel := context.WithTimeout(context.Background(), envtest.Backstop)
	defer cancel()
	buildContext, err := docker.Context(docker.Spec{Dockerfile: []byte("FROM " + baseImage + "\n")})
	if err != nil {
		t.Fatal(err)
	}
	tag := "vibesys-cli-contract-save:" + randomHex(6)
	if err := engine.Build(ctx, docker.BuildSpec{Tag: tag, Context: buildContext}, discard{}); err != nil {
		t.Fatal(err)
	}
	img, ok, err := engine.Image(ctx, tag)
	if err != nil || !ok {
		t.Fatalf("image %v, %v", ok, err)
	}
	var archive bytes.Buffer
	if err := engine.Save(ctx, img.ID, &archive); err != nil {
		t.Fatal(err)
	}
	r := tar.NewReader(&archive)
	manifest := false
	for {
		h, err := r.Next()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			t.Fatal(err)
		}
		manifest = manifest || h.Name == "manifest.json"
	}
	if !manifest {
		t.Error("the archive has no manifest.json")
	}
	err = engine.Save(ctx, "vibesys-cli-contract-save:no-such-"+randomHex(6), io.Discard)
	var f *core.Failure
	if !errors.As(err, &f) || f.Kind != core.FailureUnavailable {
		t.Errorf("saving an image that is not there: %v", err)
	}
}
