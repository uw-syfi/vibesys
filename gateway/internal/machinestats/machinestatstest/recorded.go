package machinestatstest

import (
	"context"
	"embed"
	"fmt"
)

// recorded holds real output, trimmed to the lines the parsers read: procfs from
// a Linux arm64 container, and the tools from an Apple M3 Pro. Nothing here is a
// real recording of nvidia-smi or rocm-smi, because none exists.
//
//go:embed recorded
var recorded embed.FS

func file(name string) []byte {
	data, err := recorded.ReadFile("recorded/" + name)
	if err != nil {
		panic(err)
	}
	return data
}

func reply(name string) func(context.Context, []string) ([]byte, error) {
	data := file(name)
	return func(context.Context, []string) ([]byte, error) { return data, nil }
}

// Linux is a recorded Linux machine with no GPU tools.
func Linux() *FakeHost {
	return &FakeHost{
		Tools: map[string]func(context.Context, []string) ([]byte, error){},
		Files: map[string][][]byte{
			"/proc/stat":    {file("proc-stat-1"), file("proc-stat-2"), file("proc-stat-3")},
			"/proc/meminfo": {file("proc-meminfo")},
		},
	}
}

// MacOS is a recorded Apple silicon Mac, with its tools at their fixed paths.
func MacOS() *FakeHost {
	return &FakeHost{
		Tools: map[string]func(context.Context, []string) ([]byte, error){
			"/usr/bin/top":     reply("top"),
			"/usr/sbin/sysctl": reply("sysctl-memsize"),
			"/usr/bin/vm_stat": reply("vm_stat"),
			"/usr/sbin/ioreg":  reply("ioreg"),
		},
	}
}

// Recorded returns the recorded machine for goos.
func Recorded(goos string) *FakeHost {
	switch goos {
	case "linux":
		return Linux()
	case "darwin":
		return MacOS()
	}
	panic(fmt.Sprintf("no recording for %s", goos))
}
