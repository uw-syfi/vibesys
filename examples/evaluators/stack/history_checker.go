package main

func checkScenarioHistory(s scenario, capacity int, history []recordedOperation) bool {
	switch s {
	case scenarioSPSC, scenarioMPSC, scenarioSPMC:
		return checkStackHistory(capacity, history)
	case scenarioMPMC:
		return checkReservationAwareStackHistory(capacity, history)
	default:
		return false
	}
}

func correctnessContract(s scenario) string {
	if s == scenarioMPMC {
		return "reservation-aware bounded stack"
	}
	return "linearizable bounded stack"
}
