//go:build docker && unix

package dockercli_test

import (
	"archive/tar"
	"bytes"
	"context"
	"errors"
	"io"
	"slices"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker"
	"github.com/uw-syfi/vibesys/cli/internal/environment/envtest"
)

// An image's files are exported as a container of it has them, every layer applied, its
// environment is read in its order, and the container made to export them is gone; an image that
// is not there is a failure, not an empty archive.
func TestAnImagesFilesAndEnvironmentAreExported(t *testing.T) {
	engine := newEngine()
	ctx, cancel := context.WithTimeout(context.Background(), envtest.Backstop)
	defer cancel()
	// The second layer removes a file of the first, which the export must not have.
	dockerfile := "FROM " + baseImage + "\nRUN echo one > /kept && echo two > /removed\nRUN rm /removed\nENV GREETING=\"a b\" ZED=1\n"
	buildContext, err := docker.Context(docker.Spec{Dockerfile: []byte(dockerfile)})
	if err != nil {
		t.Fatal(err)
	}
	tag := "vibesys-cli-contract-export:" + randomHex(6)
	if err := engine.Build(ctx, docker.BuildSpec{Tag: tag, Context: buildContext}, discard{}); err != nil {
		t.Fatal(err)
	}
	before, err := engine.Containers(ctx, docker.Labels{docker.Owner + ".export": "1"})
	if err != nil {
		t.Fatal(err)
	}
	var archive bytes.Buffer
	if err := engine.Export(ctx, tag, &archive); err != nil {
		t.Fatal(err)
	}
	files := map[string]string{}
	r := tar.NewReader(&archive)
	for {
		h, err := r.Next()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			t.Fatal(err)
		}
		body, _ := io.ReadAll(r)
		files[strings.TrimPrefix(h.Name, "./")] = string(body)
	}
	if files["kept"] != "one\n" {
		t.Errorf("the exported /kept holds %q", files["kept"])
	}
	if _, ok := files["removed"]; ok {
		t.Error("the export has a file a later layer removed")
	}
	env, err := engine.Env(ctx, tag)
	if err != nil {
		t.Fatal(err)
	}
	if i := slices.Index(env, "GREETING=a b"); i < 0 || i+1 >= len(env) || env[i+1] != "ZED=1" {
		t.Errorf("environment %q", env)
	}
	after, err := engine.Containers(ctx, docker.Labels{docker.Owner + ".export": "1"})
	if err != nil || len(after) > len(before) {
		t.Errorf("containers made to export left: %v, %v", after, err)
	}
	missing := "vibesys-cli-contract-export:no-such-" + randomHex(6)
	var f *core.Failure
	if err := engine.Export(ctx, missing, io.Discard); !errors.As(err, &f) || f.Kind != core.FailureUnavailable {
		t.Errorf("exporting an image that is not there: %v", err)
	}
	if _, err := engine.Env(ctx, missing); err == nil {
		t.Error("an image that is not there has an environment")
	}
}
