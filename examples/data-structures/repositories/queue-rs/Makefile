.PHONY: all clean queue-candidate.so

TARGET_DIR := $(or $(CARGO_TARGET_DIR),target)

all: queue-candidate.so

queue-candidate.so: Cargo.toml Cargo.lock src/lib.rs
	cargo build --release --locked
	cp "$(TARGET_DIR)/release/libqueue_candidate.$$(test "$$(uname -s)" = Darwin && printf dylib || printf so)" "$@"

clean:
	cargo clean
	rm -f queue-candidate.so
