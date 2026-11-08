package daemon_test

// thinnedUnderRace is how many cases of a seeded property a run goes through: all of n in the plain
// pass, which covers the input space, and a quarter under the race detector. The race pass is there
// for interleavings, which a case finds as well as the next one, and costs several times a plain
// case, so the seeds beyond a quarter bought it minutes of the orchestrator check and no new races.
func thinnedUnderRace(n uint64) uint64 {
	if underRace {
		return max(n/4, 1)
	}
	return n
}
