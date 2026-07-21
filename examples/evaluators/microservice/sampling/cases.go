// Package sampling owns deterministic evaluator sampling primitives shared
// across benchmark and accuracy modes.
package sampling

import (
	"fmt"
	"math/rand"
)

func CaseCount(seed int64, minimum, maximum int) (int, error) {
	if minimum < 1 || maximum < minimum {
		return 0, fmt.Errorf("case bounds must satisfy 1 <= min <= max, got %d..%d", minimum, maximum)
	}
	random := rand.New(rand.NewSource(seed))
	return minimum + random.Intn(maximum-minimum+1), nil
}
