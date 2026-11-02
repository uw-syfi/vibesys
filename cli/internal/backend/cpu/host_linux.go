//go:build linux

package cpu

// Host returns the System of the machine this process runs on.
func Host() System { return procHost() }
