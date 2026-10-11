pub const MIN_VALUE_SIZE: usize = 8;

pub fn prepare_payload(payload: &mut [u8], value: u64) {
    payload[..8].copy_from_slice(&value.to_le_bytes());
    let lane = (value >> 56) as u8;
    for (index, byte) in payload[8..].iter_mut().enumerate() {
        *byte = lane
            .wrapping_mul(31)
            .wrapping_add((index as u8).wrapping_mul(17))
            .wrapping_add(0x5d);
    }
}

pub fn validate_payload(payload: &[u8]) -> Result<u64, String> {
    if payload.len() < MIN_VALUE_SIZE {
        return Err(format!(
            "payload is {} bytes, want at least 8",
            payload.len()
        ));
    }
    let value = u64::from_le_bytes(payload[..8].try_into().expect("eight-byte prefix"));
    let lane = (value >> 56) as u8;
    for (index, byte) in payload[8..].iter().enumerate() {
        let expected = lane
            .wrapping_mul(31)
            .wrapping_add((index as u8).wrapping_mul(17))
            .wrapping_add(0x5d);
        if *byte != expected {
            return Err(format!(
                "payload byte {} is {}, want {}",
                index + 8,
                byte,
                expected
            ));
        }
    }
    Ok(value)
}

#[cfg(test)]
mod tests {
    use super::{prepare_payload, validate_payload};

    #[test]
    fn payload_round_trip_and_corruption_detection() {
        let value = (3_u64 << 56) | 42;
        let mut payload = vec![0_u8; 256];
        prepare_payload(&mut payload, value);
        assert_eq!(validate_payload(&payload).unwrap(), value);

        payload[128] ^= 1;
        assert!(validate_payload(&payload).is_err());
    }
}
