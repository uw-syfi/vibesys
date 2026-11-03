package nvidia_test

import (
	"maps"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/backend/nvidia"
)

const sparkSummary = `+-----------------------------------------------------------------------------------------+
| NVIDIA-SMI 580.159.03             Driver Version: 580.159.03     CUDA Version: 13.0     |
+-----------------------------------------+------------------------+----------------------+`

func TestTheFactsOfASparkAreItsGPUDriverAndCUDA(t *testing.T) {
	got, err := nvidia.Parse("NVIDIA GB10, 580.159.03\n", sparkSummary)
	want := map[string]string{"gpu_model": "NVIDIA GB10", "gpu_count": "1", "gpu_driver": "580.159.03", "cuda_driver": "13.0"}
	if err != nil || !maps.Equal(got, want) {
		t.Errorf("%v, %v; want %v", got, err, want)
	}
	got, err = nvidia.Parse("NVIDIA H100, 550.1\nNVIDIA H100, 550.1\n", "CUDA Version: 12.4")
	if err != nil || got["gpu_count"] != "2" {
		t.Errorf("two GPUs: %v, %v", got, err)
	}
}

func TestGPUsThatCannotBeNamedAsOneAreRefused(t *testing.T) {
	for name, c := range map[string][2]string{
		"no GPU":          {"", sparkSummary},
		"two models":      {"NVIDIA H100, 550.1\nNVIDIA A100, 550.1\n", sparkSummary},
		"no driver":       {"NVIDIA GB10\n", sparkSummary},
		"no CUDA version": {"NVIDIA GB10, 580.1\n", "Driver Version: 580.1"},
		"an empty name":   {", 580.1\n", sparkSummary},
	} {
		if got, err := nvidia.Parse(c[0], c[1]); err == nil {
			t.Errorf("%s: %v, want a refusal", name, got)
		}
	}
}
