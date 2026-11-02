package runtimedir

import "syscall"

// BootID names this boot of the machine: it stays the same until the machine restarts.
func BootID() (string, error) { return syscall.Sysctl("kern.bootsessionuuid") }
