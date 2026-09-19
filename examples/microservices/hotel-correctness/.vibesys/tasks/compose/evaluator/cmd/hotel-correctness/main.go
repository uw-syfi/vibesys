package main

import (
	"fmt"
	"os"

	"vibesys/hotel-correctness-evaluator/internal/hotel"
	"vibesys/microservice-evaluator/composition"
	"vibesys/microservice-evaluator/drivers/httpdriver"
	"vibesys/microservice-evaluator/servicebenchcli"
)

var version = "dev"

func main() {
	err := servicebenchcli.Run(
		os.Args[1:],
		version,
		composition.Driver(httpdriver.New()),
		composition.AccuracyApplication("hotel", hotel.New),
	)
	if err != nil {
		fmt.Fprintln(os.Stderr, "hotel-correctness:", err)
		os.Exit(1)
	}
}
