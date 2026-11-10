//! Latency distributions, computed exactly as the evaluator's telemetry
//! package computes them so that both report the same numbers.

use serde::Serialize;

#[derive(Clone, Copy, Debug, Default, PartialEq, Serialize)]
pub struct Distribution {
    pub count: usize,
    pub error_count: usize,
    pub mean_ms: f64,
    pub p50_ms: f64,
    pub p95_ms: f64,
    pub p99_ms: f64,
    pub max_ms: f64,
}

#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub(crate) struct Sample {
    pub ms: f64,
    pub failed: bool,
}

/// Convert a count to `f64`. Lossless below 2^53, which bounds every count
/// and every span duration in nanoseconds (about 104 days); `f64::from` has no
/// `u64` or `usize` impl, so the cast is the only lossless-in-practice option.
#[allow(clippy::cast_precision_loss)]
pub(crate) fn float(value: u64) -> f64 {
    value as f64
}

pub(crate) fn count(value: usize) -> f64 {
    float(u64::try_from(value).unwrap_or(u64::MAX))
}

/// Milliseconds from `start` to `end` in nanoseconds (zero if reversed).
pub(crate) fn duration_ms(start: u64, end: u64) -> f64 {
    float(end.saturating_sub(start)) / 1e6
}

/// Linear interpolation between closest ranks; equal neighbors return their
/// exact value so that p50 <= p95 <= p99 holds on plateaus.
pub(crate) fn percentile(sorted: &[f64], percent: f64) -> f64 {
    if sorted.len() == 1 {
        return sorted[0];
    }
    let position = count(sorted.len() - 1) * percent / 100.0;
    // `position` is finite and in [0, len - 1]; flooring it to an index is
    // the rank selection itself, which no conversion trait expresses.
    #[allow(clippy::cast_possible_truncation, clippy::cast_sign_loss)]
    let lower = position as usize;
    let upper = lower + 1;
    if upper >= sorted.len() {
        return sorted[lower];
    }
    // Exact equality is the point: interpolating between equal neighbors can
    // round differently at different positions.
    #[allow(clippy::float_cmp)]
    if sorted[lower] == sorted[upper] {
        return sorted[lower];
    }
    let fraction = position - count(lower);
    sorted[lower] * (1.0 - fraction) + sorted[upper] * fraction
}

impl Distribution {
    /// Summarize samples; an empty slice yields an all-zero distribution.
    pub(crate) fn of(samples: &[Sample]) -> Self {
        if samples.is_empty() {
            return Self::default();
        }
        let mut values: Vec<f64> = samples.iter().map(|s| s.ms).collect();
        values.sort_by(f64::total_cmp);
        let mut sum = 0.0;
        for value in &values {
            sum += value;
        }
        let mean = sum / count(values.len());
        Self {
            count: values.len(),
            error_count: samples.iter().filter(|s| s.failed).count(),
            mean_ms: mean,
            p50_ms: percentile(&values, 50.0),
            p95_ms: percentile(&values, 95.0),
            p99_ms: percentile(&values, 99.0),
            max_ms: values[values.len() - 1],
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn percentiles_interpolate_and_respect_plateaus() {
        let values = [1.0, 2.0, 3.0, 4.0, 5.0];
        assert!((percentile(&values, 50.0) - 3.0).abs() < 1e-12);
        assert!((percentile(&values, 95.0) - 4.8).abs() < 1e-12);
        let plateau = [0.1; 7];
        assert!(percentile(&plateau, 50.0) <= percentile(&plateau, 95.0));
        assert!((percentile(&[7.0], 99.0) - 7.0).abs() < 1e-12);
    }

    #[test]
    fn distribution_counts_errors_and_orders_percentiles() {
        let samples: Vec<Sample> = (1..=100)
            .map(|i| Sample {
                ms: f64::from(i),
                failed: i % 10 == 0,
            })
            .collect();
        let d = Distribution::of(&samples);
        assert_eq!((d.count, d.error_count), (100, 10));
        assert!(d.p50_ms <= d.p95_ms && d.p95_ms <= d.p99_ms && d.p99_ms <= d.max_ms);
        assert!((d.mean_ms - 50.5).abs() < 1e-12);
    }
}
