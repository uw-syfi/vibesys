package core_test

import (
	"fmt"
	"math/rand/v2"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// probeSpelling writes an alias the way a probe or a gres could: in a random case, behind a vendor
// prefix, and before a memory or form-factor suffix. Every spelling keeps the alias a whole token.
func probeSpelling(rng *rand.Rand, v core.AcceleratorVendor, alias string) string {
	prefixes := map[core.AcceleratorVendor][]string{
		core.VendorNVIDIA: {"", "NVIDIA ", "nvidia_", "NVIDIA Tesla "},
		core.VendorAMD:    {"", "AMD ", "AMD Instinct ", "Instinct "},
	}[v]
	suffixes := []string{"", " 80GB HBM3", "-SXM4-80GB", "_192G", " PCIe", "-40GB", " (rev a1)"}
	name := alias
	switch rng.IntN(3) {
	case 0:
		name = strings.ToUpper(alias)
	case 1:
		name = strings.ToLower(alias)
	}
	return prefixes[rng.IntN(len(prefixes))] + name + suffixes[rng.IntN(len(suffixes))]
}

func probedVendors() []core.AcceleratorVendor {
	return []core.AcceleratorVendor{core.VendorNVIDIA, core.VendorAMD}
}

// Every alias of every row, in any generated spelling, reads back as the row's model.
func TestEveryAliasParsesBackToItsModel(t *testing.T) {
	rng := rand.New(rand.NewPCG(11, 12))
	aliased := 0
	for _, row := range core.Accelerators() {
		for _, alias := range row.Aliases {
			aliased++
			for range 200 {
				name := probeSpelling(rng, row.Vendor, alias)
				got, err := core.ParseAccelerator(row.Vendor, name, "4")
				if err != nil || got.Model != row.Model || got.Vendor != row.Vendor || got.DeviceName != name || got.Count != 4 {
					t.Fatalf("%s as %q: %+v, %v", row.Display, name, got, err)
				}
			}
		}
	}
	if aliased == 0 {
		t.Fatal("the registry has no aliases")
	}
}

// The registry is one table: a display name, a model and an alias each name one row, and an alias
// is a lower-case token a vendor cannot spell for two models.
func TestRegistryRowsAreDistinct(t *testing.T) {
	displays, models, aliases := map[string]bool{}, map[core.AcceleratorModel]bool{}, map[string]string{}
	for _, row := range core.Accelerators() {
		if displays[row.Display] {
			t.Errorf("display %q is in two rows", row.Display)
		}
		displays[row.Display] = true
		if row.Model != "" {
			if models[row.Model] {
				t.Errorf("model %q is in two rows", row.Model)
			}
			models[row.Model] = true
		}
		if row.Model == "" && len(row.Aliases) > 0 {
			t.Errorf("%s names no model and has aliases", row.Display)
		}
		for _, a := range row.Aliases {
			key := fmt.Sprint(row.Vendor, "/", a)
			if a != strings.ToLower(a) || strings.ContainsFunc(a, func(r rune) bool { return !(r >= 'a' && r <= 'z' || r >= '0' && r <= '9') }) {
				t.Errorf("alias %q of %s is not a lower-case token", a, row.Display)
			}
			if other, ok := aliases[key]; ok {
				t.Errorf("alias %q is of %s and %s", a, other, row.Display)
			}
			aliases[key] = row.Display
		}
	}
}

type deviceText struct {
	vendor core.AcceleratorVendor
	name   string
}

// Text the registry does not name is unknown, with the raw text kept, and never a near match: a
// longer token, a device of the other vendor, a name of two models, and noise.
func TestUnknownDeviceTextIsUnknownAndKeepsTheRawText(t *testing.T) {
	rng := rand.New(rand.NewPCG(21, 22))
	var cases []deviceText
	for _, row := range core.Accelerators() {
		for _, alias := range row.Aliases {
			for _, extra := range []string{"0", "s", "x2", "9"} {
				cases = append(cases, deviceText{row.Vendor, probeSpelling(rng, row.Vendor, alias+extra)})
			}
			cases = append(cases, deviceText{row.Vendor, probeSpelling(rng, row.Vendor, alias[:len(alias)-1])})
			for _, other := range probedVendors() {
				if other != row.Vendor {
					cases = append(cases, deviceText{other, probeSpelling(rng, row.Vendor, alias)})
				}
			}
		}
	}
	cases = append(cases,
		deviceText{core.VendorNVIDIA, "NVIDIA H100 and A100"},
		deviceText{core.VendorAMD, "AMD MI300X MI325X"},
		deviceText{core.VendorNVIDIA, "NVIDIA H200"},
		deviceText{core.VendorNVIDIA, "NVIDIA L40S"},
		deviceText{core.VendorAMD, "gfx942"},
		deviceText{core.VendorNVIDIA, "???"},
	)
	if len(cases) < 20 {
		t.Fatalf("only %d cases", len(cases))
	}
	for _, c := range cases {
		got, err := core.ParseAccelerator(c.vendor, c.name, "1")
		if err != nil || got.Model != core.ModelOther || got.DeviceName != c.name {
			t.Errorf("%v %q: %+v, %v", c.vendor, c.name, got, err)
		}
	}
}

// A display name is matched exactly: another spelling of it is not found.
func TestLookupAcceleratorIsExact(t *testing.T) {
	for _, row := range core.Accelerators() {
		got, ok := core.LookupAccelerator(row.Display)
		if !ok || got.Model != row.Model || got.Vendor != row.Vendor {
			t.Errorf("%q: %+v, %v", row.Display, got, ok)
		}
		if _, ok := core.LookupAccelerator(strings.ToLower(row.Display) + " "); ok {
			t.Errorf("%q was found in another spelling", row.Display)
		}
	}
}
