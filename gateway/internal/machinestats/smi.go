package machinestats

import (
	"context"
	"errors"
	"strconv"
	"strings"

	apiv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/api/v1"
)

// nvidiaQuery is vs_sandbox.gpu_monitor's query, held to it by the shared fixture.
var nvidiaQuery = []string{
	"--query-gpu=index,uuid,name,memory.used,memory.total,utilization.gpu",
	"--format=csv,noheader,nounits",
}

const (
	nvidiaColumns = 6
	mebibyte      = 1 << 20
)

// smiGpus reads NVIDIA GPUs. rocm-smi is reported as present but unread: no
// recording of its output exists, and its format is not guessed.
func smiGpus(ctx context.Context, host Host) (*apiv1.GpuList, error) {
	nvidia, err := host.Find("nvidia-smi", "/usr/bin/nvidia-smi")
	if errors.Is(err, ErrToolNotFound) {
		if _, err := host.Find("rocm-smi", "/usr/bin/rocm-smi", "/opt/rocm/bin/rocm-smi"); err == nil {
			return nil, errUnverified
		}
		return nil, err
	}
	if err != nil {
		return nil, err
	}
	output, err := host.Run(ctx, nvidia, nvidiaQuery...)
	if err != nil {
		return nil, err
	}
	return parseNvidia(output)
}

// parseNvidia refuses the whole output on any bad row. Python's run-side picker
// drops such rows because it only needs a usable GPU. A meter that silently
// loses one would read as a machine with fewer GPUs. It parses and does not
// bound: whether the list may be sent is the schema's rule, applied to the section.
func parseNvidia(output []byte) (*apiv1.GpuList, error) {
	gpus := &apiv1.GpuList{}
	for line := range strings.Lines(strings.TrimSpace(string(output))) {
		columns := strings.Split(line, ",")
		if len(columns) != nvidiaColumns {
			return nil, parseFailure("nvidia-smi row has %d columns", len(columns))
		}
		for index := range columns {
			columns[index] = strings.TrimSpace(columns[index])
		}
		index, indexErr := strconv.ParseUint(columns[0], 10, 32)
		used, usedErr := strconv.ParseUint(columns[3], 10, 44)
		total, totalErr := strconv.ParseUint(columns[4], 10, 44)
		busy, busyErr := strconv.ParseUint(columns[5], 10, 8)
		if indexErr != nil || usedErr != nil || totalErr != nil || busyErr != nil {
			return nil, parseFailure("nvidia-smi row has a non-numeric column")
		}
		gpus.Gpus = append(gpus.Gpus, &apiv1.GpuStats{
			Index: uint32(index), Name: columns[2], UtilizationPercent: float64(busy),
			MemoryUsedBytes: used * mebibyte, MemoryTotalBytes: total * mebibyte,
		})
	}
	return gpus, nil
}
