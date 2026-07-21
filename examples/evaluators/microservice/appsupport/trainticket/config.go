package trainticket

import "fmt"

type Config struct {
	Records int
}

// ParseConfig owns fields shared by Train Ticket benchmark and accuracy modes.
// Accuracy may not use every value, but it must reject the same malformed
// workload rather than allowing configuration validity to depend on mode.
func ParseConfig(values map[string]any) (Config, error) {
	config := Config{Records: 32}
	for key := range values {
		if key != "records" {
			return Config{}, fmt.Errorf("unknown Train Ticket application_config field %q", key)
		}
	}
	if raw, exists := values["records"]; exists {
		records, ok := integer(raw)
		if !ok || records < 2 {
			return Config{}, fmt.Errorf("application_config.records must be an integer greater than one")
		}
		config.Records = records
	}
	return config, nil
}

func integer(value any) (int, bool) {
	switch number := value.(type) {
	case int64:
		return int(number), int64(int(number)) == number
	case int:
		return number, true
	case float64:
		return int(number), number == float64(int(number))
	default:
		return 0, false
	}
}
