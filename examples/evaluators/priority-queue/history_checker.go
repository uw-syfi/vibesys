package main

func checkScenarioHistory(s scenario, capacity int, history []recordedOperation) bool {
	switch s {
	case scenarioSPSC, scenarioMPSC, scenarioSPMC:
		return checkPriorityHistory(capacity, history)
	case scenarioMPMC:
		return checkReservationAwarePriorityHistory(capacity, history)
	default:
		return false
	}
}

func correctnessContract(s scenario) string {
	if s == scenarioMPMC {
		return "reservation-aware bounded priority queue"
	}
	return "linearizable bounded priority queue"
}
