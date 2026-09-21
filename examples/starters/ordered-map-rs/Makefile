.PHONY: all clean ordered-map-candidate.so

TARGET_DIR := $(or $(CARGO_TARGET_DIR),target)

all: ordered-map-candidate.so

ordered-map-candidate.so: Cargo.toml Cargo.lock src/lib.rs src/ffi.rs
	cargo build --release --locked
	cp "$(TARGET_DIR)/release/libordered_map_candidate.$$(test "$$(uname -s)" = Darwin && printf dylib || printf so)" "$@"

clean:
	cargo clean
	rm -f ordered-map-candidate.so
