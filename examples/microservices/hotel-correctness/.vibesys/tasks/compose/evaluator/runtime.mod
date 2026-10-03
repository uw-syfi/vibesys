module vibesys/hotel-correctness-evaluator

go 1.21

require vibesys/microservice-evaluator v0.0.0

require (
	github.com/BurntSushi/toml v1.4.0 // indirect
	github.com/uw-syfi/vibesys/sdk/vs-evaluator/vseval v0.2.0 // indirect
)

replace vibesys/microservice-evaluator => ../_evaluator/microservice
