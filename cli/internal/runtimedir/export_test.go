package runtimedir

// ResolveMachineIn is ResolveMachine with the places it looks at moved under a test's directory.
var ResolveMachineIn = resolveMachine

// ResolveLeasesIn is ResolveLeases with the parents it tries given by a test.
var ResolveLeasesIn = resolveLeases

// LocalDiskAdvice is the advice a runtime directory on a network file system gets.
var LocalDiskAdvice = localDiskAdvice
