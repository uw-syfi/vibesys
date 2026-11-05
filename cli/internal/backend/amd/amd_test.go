package amd_test

import (
	"maps"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/backend/amd"
)

// rocminfo is the part of rocminfo's output Script keeps, for a node of one CPU and n MI210s; each
// GPU agent has an ISA section whose own Name line must not be taken for an agent.
func rocminfo(n int) string {
	var b strings.Builder
	b.WriteString("  Name:                    AMD EPYC 7V13 64-Core Processor\n  Marketing Name:          AMD EPYC 7V13 64-Core Processor\n  Device Type:             CPU\n")
	for range n {
		b.WriteString("  Name:                    gfx90a\n  Marketing Name:          AMD Instinct MI210\n  Device Type:             GPU\n")
		b.WriteString("      Name:                    amdgcn-amd-amdhsa--gfx90a:sramecc+:xnack-\n")
	}
	return b.String()
}

const smi = `GPU: 0
        MARKET_NAME: AMD Instinct MI210
        VERSION: 6.16.13
`

func output(driver, info, smiOut string) string {
	return "==driver==\n" + driver + "\n==rocminfo==\n" + info + "==amd-smi==\n" + smiOut
}

func TestTheFactsOfANodeAreItsGPUsAndItsDriver(t *testing.T) {
	for name, c := range map[string]struct {
		out  string
		want map[string]string
	}{
		"one MI210":            {output("6.16.13", rocminfo(1), smi), map[string]string{amd.FactModel: "AMD Instinct MI210", amd.FactCount: "1", amd.FactDriver: "6.16.13"}},
		"four":                 {output("6.16.13", rocminfo(4), ""), map[string]string{amd.FactModel: "AMD Instinct MI210", amd.FactCount: "4", amd.FactDriver: "6.16.13"}},
		"amd-smi alone":        {output("", "", smi), map[string]string{amd.FactModel: "AMD Instinct MI210", amd.FactCount: "1", amd.FactDriver: "6.16.13"}},
		"rocminfo, no sysfs":   {output("", rocminfo(2), smi), map[string]string{amd.FactModel: "AMD Instinct MI210", amd.FactCount: "2", amd.FactDriver: "6.16.13"}},
		"sysfs wins on driver": {output("6.12.12", rocminfo(1), smi), map[string]string{amd.FactModel: "AMD Instinct MI210", amd.FactCount: "1", amd.FactDriver: "6.12.12"}},
	} {
		got, err := amd.Parse(c.out)
		if err != nil || !maps.Equal(got, c.want) {
			t.Errorf("%s: %v, %v; want %v", name, got, err, c.want)
		}
	}
}

func TestGPUsThatCannotBeNamedAsOneAreRefused(t *testing.T) {
	mixed := rocminfo(1) + "  Name:                    gfx942\n  Marketing Name:          AMD Instinct MI300X\n  Device Type:             GPU\n"
	for name, out := range map[string]string{
		"no GPU":     output("6.16.13", rocminfo(0), ""),
		"no driver":  output("", rocminfo(1), ""),
		"two models": output("6.16.13", mixed, ""),
		"nothing":    "",
	} {
		if got, err := amd.Parse(out); err == nil {
			t.Errorf("%s: %v, want a refusal", name, got)
		}
	}
}
