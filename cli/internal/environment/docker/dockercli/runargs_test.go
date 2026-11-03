package dockercli

import (
	"slices"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/environment/docker"
)

func TestGPUsArePassedByTheFlagOrByTheRuntimeNeverByCDI(t *testing.T) {
	base := docker.ContainerSpec{Name: "vibesys-cli-0123456789", Image: "sha256:0000000000000000000000000000000000000000000000000000000000000000", Dir: "/w"}
	cases := []struct {
		gpus      docker.GPUs
		want, not []string
	}{
		{docker.GPUs{}, nil, []string{"--gpus", "--runtime", "--device"}},
		{docker.GPUs{Devices: "all"}, []string{"--gpus", "all"}, []string{"--runtime", "--device"}},
		{docker.GPUs{Devices: "device=0", Runtime: "nvidia"}, []string{"--runtime", "nvidia", "NVIDIA_VISIBLE_DEVICES=0"}, []string{"--gpus", "--device"}},
		{docker.GPUs{Devices: "all", Runtime: "nvidia"}, []string{"--runtime", "nvidia", "NVIDIA_VISIBLE_DEVICES=all"}, []string{"--gpus", "--device"}},
	}
	for _, c := range cases {
		spec := base
		spec.GPUs = c.gpus
		if err := spec.Validate(); err != nil {
			t.Fatalf("%+v: %v", c.gpus, err)
		}
		args := runArgs(spec)
		for i := 0; i+1 < len(c.want); i += 2 {
			at := slices.Index(args, c.want[i])
			if at < 0 || (len(c.want) > i+1 && !slices.Contains(args[at:], c.want[i+1])) {
				t.Errorf("%+v: args %v lack %v", c.gpus, args, c.want)
			}
		}
		if c.gpus.Runtime != "" && !slices.Contains(args, c.want[2]) {
			t.Errorf("%+v: args %v lack %s", c.gpus, args, c.want[2])
		}
		for _, flag := range c.not {
			if slices.Contains(args, flag) {
				t.Errorf("%+v: args %v hold %s", c.gpus, args, flag)
			}
		}
		// The image and the keeper's command stay last, so a flag is never read as the command.
		if args[len(args)-3] != spec.Image {
			t.Errorf("%+v: the image is not where the command starts: %v", c.gpus, args)
		}
	}
}
