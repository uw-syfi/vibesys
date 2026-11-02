package runtimedir

import (
	"os"
	"strings"
)

// BootID names this boot of the machine: it stays the same until the machine restarts.
func BootID() (string, error) {
	data, err := os.ReadFile("/proc/sys/kernel/random/boot_id")
	if err != nil {
		return "", err
	}
	return strings.TrimSpace(string(data)), nil
}
