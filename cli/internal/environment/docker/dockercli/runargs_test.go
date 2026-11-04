package dockercli

import (
	"slices"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/environment/docker"
)

// test-isolation: runArgs is the one place a ContainerSpec becomes docker run's flags, and the
// flags of GPUs and of the network are not observable through the Fake.
func TestGPUsAndTheNetworkArePassedByTheirFlagsNeverByCDI(t *testing.T) {
	base := docker.ContainerSpec{Name: "vibesys-cli-0123456789", Image: "sha256:0000000000000000000000000000000000000000000000000000000000000000", Dir: "/w"}
	cases := []struct {
		gpus      docker.GPUs
		network   string
		want, not []string
	}{
		{docker.GPUs{}, "", nil, []string{"--gpus", "--runtime", "--device", "--network"}},
		{docker.GPUs{Devices: "all"}, "", []string{"--gpus", "all"}, []string{"--runtime", "--device"}},
		{docker.GPUs{Devices: "device=0", Runtime: "nvidia"}, "host", []string{"--runtime", "nvidia", "--network", "host"}, []string{"--gpus", "--device"}},
		{docker.GPUs{}, "kind", []string{"--network", "kind"}, []string{"--gpus", "--runtime"}},
	}
	for _, c := range cases {
		spec := base
		spec.GPUs, spec.Network = c.gpus, c.network
		if err := spec.Validate(); err != nil {
			t.Fatalf("%+v: %v", c, err)
		}
		args := runArgs(spec)
		for i := 0; i+1 < len(c.want); i += 2 {
			at := slices.Index(args, c.want[i])
			if at < 0 || at+1 >= len(args) || args[at+1] != c.want[i+1] {
				t.Errorf("%+v: args %v lack %s %s", c, args, c.want[i], c.want[i+1])
			}
		}
		for _, flag := range c.not {
			if slices.Contains(args, flag) {
				t.Errorf("%+v: args %v hold %s", c, args, flag)
			}
		}
		// The image and the keeper's command stay last, so a flag is never read as the command.
		if args[len(args)-3] != spec.Image {
			t.Errorf("%+v: the image is not where the command starts: %v", c, args)
		}
	}
}
