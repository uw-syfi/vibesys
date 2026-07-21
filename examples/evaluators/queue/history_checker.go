package main

func checkScenarioHistory(s scenario, capacity int, history []recordedOperation) bool {
	switch s {
	case scenarioSPSC, scenarioMPSC:
		return checkExactFIFOHistory(capacity, history)
	case scenarioMPMC:
		return checkReservationAwareFIFOHistory(capacity, history)
	default:
		return false
	}
}

func correctnessContract(s scenario) string {
	if s == scenarioMPMC {
		return "reservation-aware bounded FIFO"
	}
	return "linearizable bounded FIFO"
}
