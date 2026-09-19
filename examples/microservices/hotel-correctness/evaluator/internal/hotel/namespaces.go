package hotel

import "time"

// Reservation fixtures are permanent: the public API has no delete operation.
// Every check therefore owns a disjoint day range inside the hidden, seeded
// date namespace. Offsets are spaced far enough apart that raising the case
// count cannot make two checks share a night.
const (
	nightsPerCase = 4

	concurrentIsolationOffset = 2000
	concurrentCapacityOffset  = 2100
	durabilityOffset          = 2400
	degenerateOffset          = 2700
	multiNightOffset          = 2800
	livenessOffset            = 2900
)

func namespacedNight(seed int64, dayOffset int) [2]string {
	start := reservationDate(seed).AddDate(0, 0, dayOffset)
	return [2]string{start.Format(time.DateOnly), start.AddDate(0, 0, 1).Format(time.DateOnly)}
}

// maxAccuracyCases is the largest case count whose per-case reservation
// ranges remain disjoint. Multi-night cases are the binding range: case 25
// owns [2896, 2900), and the endpoint-liveness fixture begins at 2900.
const maxAccuracyCases = (livenessOffset - multiNightOffset) / nightsPerCase
