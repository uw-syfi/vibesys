package resource_test

import (
	"fmt"
	"maps"
	"math/rand/v2"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/resource"
)

// Attachments that ask for nothing twice combine into their union; two that ask for one container
// path, one variable with two values, two networks or two sets of GPUs are refused.
func TestCombiningAttachmentsKeepsAllOrRefusesAClash(t *testing.T) {
	r := rand.New(rand.NewPCG(13, 14))
	for range 2000 {
		var all []resource.Attachment
		mounts, env, network, gpus := map[string]bool{}, map[string]string{}, "", resource.GPUs{}
		clash := false
		for range 1 + r.IntN(3) {
			a := resource.Attachment{Env: map[string]string{}}
			for range r.IntN(3) {
				c := fmt.Sprintf("/c%d", r.IntN(4))
				clash = clash || mounts[c]
				mounts[c] = true
				a.Mounts = append(a.Mounts, resource.Mount{Host: "/h", Container: c})
			}
			for range r.IntN(3) {
				k, v := fmt.Sprintf("V%d", r.IntN(3)), fmt.Sprintf("%d", r.IntN(2))
				if _, ok := a.Env[k]; ok {
					continue
				}
				if old, ok := env[k]; ok && old != v {
					clash = true
				}
				env[k] = v
				a.Env[k] = v
			}
			if r.IntN(3) == 0 {
				a.Network = []string{"host", "kind"}[r.IntN(2)]
				clash = clash || (network != "" && network != a.Network)
				network = a.Network
			}
			if r.IntN(4) == 0 {
				a.GPUs = resource.GPUs{Devices: []string{"all", "device=0"}[r.IntN(2)]}
				clash = clash || (gpus != resource.GPUs{} && gpus != a.GPUs)
				gpus = a.GPUs
			}
			all = append(all, a)
		}
		got, err := resource.Combine(all)
		if clash {
			if err == nil {
				t.Fatalf("%+v combined into %+v, want a refusal", all, got)
			}
			continue
		}
		if err != nil || len(got.Mounts) != len(mounts) || !maps.Equal(got.Env, env) || got.Network != network || got.GPUs != gpus {
			t.Fatalf("%+v combined into %+v, %v", all, got, err)
		}
	}
}
