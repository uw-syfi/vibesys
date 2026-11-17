package fakeresource_test

import (
	"strconv"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/resource"
	"github.com/uw-syfi/vibesys/cli/internal/resource/devicestest"
	"github.com/uw-syfi/vibesys/cli/internal/resource/fakeresource"
	"github.com/uw-syfi/vibesys/cli/internal/resource/resourcetest"
)

func TestTheFakePassesTheContract(t *testing.T) {
	for _, kind := range core.ResourceKinds() {
		t.Run(string(kind), func(t *testing.T) {
			resourcetest.Run(t, func(*testing.T) resourcetest.Subject {
				f := &fakeresource.Fake{
					Of: kind, Facts: map[string]string{string(kind) + "_id": "x"},
					Attached: resource.Attachment{Env: map[string]string{"FAKE": "1"}, Mounts: []resource.Mount{{Host: "/h", Container: "/c", ReadOnly: true}}},
					Claimed:  []core.Claim{{Resource: string(kind) + ":x", Amount: 1}},
				}
				return resourcetest.Subject{Resource: f, Kind: kind, Break: f.Break}
			})
		})
	}
}

func TestTheDevicesFakePassesTheContract(t *testing.T) {
	for _, size := range []int{1, 2, 8, core.MaxPoolSize} {
		t.Run(strconv.Itoa(size), func(t *testing.T) {
			devicestest.Run(t, func(t *testing.T) devicestest.Subject {
				n, err := core.ParseGPUCount(uint32(size), "test")
				if err != nil {
					t.Fatal(err)
				}
				return devicestest.Subject{Devices: &fakeresource.Devices{Of: core.ResourceGPU, ID: "gpu:host-a", Size: n}, Kind: core.ResourceGPU}
			})
		})
	}
}
