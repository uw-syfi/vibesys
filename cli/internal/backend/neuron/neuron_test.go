package neuron_test

import (
	"fmt"
	"maps"
	"os/exec"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/backend/neuron"
)

// device is one entry of neuron-ls --json-output, as a trn2 host prints it.
func device(index, cores, lnc int) string {
	return fmt.Sprintf(`    {
        "instance_type": "trn2.48xlarge",
        "neuron_device": %d,
        "bdf": "0000:33:00.0",
        "connected_to": null,
        "nc_count": %d,
        "logical_neuroncore_config": %d,
        "memory_size": 103079215104,
        "neuroncore_ids": [0, 1, 2, 3],
        "neuron_processes": []
    }`, index, cores, lnc)
}

func output(driver, model string, devices ...string) string {
	return "==driver==\n" + driver + "\n==model==\n" + model + "\n==neuron-ls==\n[\n" + strings.Join(devices, ",\n") + "\n]\n"
}

func TestTheFactsOfAMachineAreItsDevicesCoresAndDriver(t *testing.T) {
	for name, c := range map[string]struct {
		out  string
		want map[string]string
	}{
		"a trn2.3xlarge": {output("2.31.7.0", "Trainium2", device(0, 4, 2)), map[string]string{
			neuron.FactModel: "Trainium2", neuron.FactDevices: "1", neuron.FactCores: "4", neuron.FactCoresPerDevice: "4",
			neuron.FactLogicalCoreConfig: "2", neuron.FactDriver: "2.31.7.0",
		}},
		"four devices listed out of order, of one core per physical core": {output("2.31.7.0", "Trainium2", device(2, 8, 1), device(0, 8, 1), device(3, 8, 1), device(1, 8, 1)), map[string]string{
			neuron.FactModel: "Trainium2", neuron.FactDevices: "4", neuron.FactCores: "32", neuron.FactCoresPerDevice: "8",
			neuron.FactLogicalCoreConfig: "1", neuron.FactDriver: "2.31.7.0",
		}},
		"no model in sysfs": {output("2.31.7.0", "", device(0, 2, 1)), map[string]string{
			neuron.FactModel: "unknown", neuron.FactDevices: "1", neuron.FactCores: "2", neuron.FactCoresPerDevice: "2",
			neuron.FactLogicalCoreConfig: "1", neuron.FactDriver: "2.31.7.0",
		}},
	} {
		got, err := neuron.Parse(c.out)
		if err != nil || !maps.Equal(got, c.want) {
			t.Errorf("%s: %v, %v; want %v", name, got, err, c.want)
		}
	}
}

// A machine whose devices cannot be numbered as one pool, or that lists none, is refused rather
// than read as some other machine.
func TestDevicesThatCannotBeOnePoolAreRefused(t *testing.T) {
	for name, out := range map[string]string{
		"nothing":                       "",
		"no driver":                     output("", "Trainium2", device(0, 4, 2)),
		"no neuron-ls":                  "==driver==\n2.31.7.0\n==model==\nTrainium2\n==neuron-ls==\n",
		"no device":                     output("2.31.7.0", "Trainium2"),
		"not JSON":                      "==driver==\n2.31.7.0\n==model==\nTrainium2\n==neuron-ls==\nNEURON DEVICE 0\n",
		"devices of unlike core counts": output("2.31.7.0", "Trainium2", device(0, 4, 2), device(1, 8, 1)),
		"a device numbered twice":       output("2.31.7.0", "Trainium2", device(0, 4, 2), device(0, 4, 2)),
		"a gap in the numbers":          output("2.31.7.0", "Trainium2", device(0, 4, 2), device(2, 4, 2)),
		"a device of no core":           output("2.31.7.0", "Trainium2", device(0, 0, 2)),
		"a device that says nothing":    output("2.31.7.0", "Trainium2", "{}"),
	} {
		if got, err := neuron.Parse(out); err == nil {
			t.Errorf("%s: %v, want a refusal", name, got)
		}
	}
}

// The script is a POSIX shell script: a shell reads it without a syntax error.
func TestTheScriptIsAShellScript(t *testing.T) {
	if out, err := exec.Command("sh", "-n", "-c", neuron.Script).CombinedOutput(); err != nil {
		t.Fatalf("sh -n: %v: %s", err, out)
	}
}
