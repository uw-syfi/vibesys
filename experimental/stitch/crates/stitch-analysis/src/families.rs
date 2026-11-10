//! Detection of operation names that embed request data, such as
//! `mongodb_get_number687684_2028-09-19`, so that one operation does not
//! split into thousands of single-call rows and graph nodes.
//!
//! Within a service, a name's key is its text before the first alphanumeric
//! token that contains a digit and starts at or after `MIN_PREFIX`. Names
//! sharing a key form a family when there are at least `MIN_FAMILY_NAMES` of
//! them, they average at most `MAX_SPANS_PER_NAME` spans each (request data
//! rarely repeats), and their longest common prefix has at least `MIN_PREFIX`
//! characters. A family is aggregated as `<common prefix>*`.

use crate::spans::SpanRef;
use serde::Serialize;
use std::collections::{BTreeMap, HashMap};

pub const MIN_FAMILY_NAMES: usize = 50;
pub const MIN_PREFIX: usize = 8;
pub const MAX_SPANS_PER_NAME: usize = 10;

/// Operation names of one service aggregated under one pattern.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct OperationFamily {
    pub service: String,
    /// The shared prefix followed by `*`; the aggregated operation name.
    pub pattern: String,
    pub distinct_names: usize,
    pub spans: usize,
    pub examples: Vec<String>,
}

/// The family key of `name`, or `None` if it has no late digit-bearing token.
fn key(name: &str) -> Option<&str> {
    let mut token_start = None;
    for (index, character) in name.char_indices() {
        if character.is_ascii_alphanumeric() {
            let start = *token_start.get_or_insert(index);
            if character.is_ascii_digit() && start >= MIN_PREFIX {
                return Some(&name[..start]);
            }
        } else {
            token_start = None;
        }
    }
    None
}

fn common_prefix<'a>(left: &'a str, right: &str) -> &'a str {
    let end = left
        .char_indices()
        .zip(right.chars())
        .find(|((_, a), b)| a != b)
        .map_or(left.len().min(right.len()), |((index, _), _)| index);
    &left[..end]
}

/// Families found among `spans`, and the aggregated name for each member
/// `(service, operation)`.
pub(crate) fn detect<'g>(
    spans: &[SpanRef<'g>],
) -> (HashMap<(&'g str, &'g str), String>, Vec<OperationFamily>) {
    let mut counts: HashMap<(&'g str, &'g str), usize> = HashMap::new();
    for span in spans {
        *counts.entry((span.service, span.operation)).or_default() += 1;
    }
    let mut groups: BTreeMap<(&str, &str), Vec<(&str, usize)>> = BTreeMap::new();
    for (&(service, operation), &count) in &counts {
        if let Some(key) = key(operation) {
            groups
                .entry((service, key))
                .or_default()
                .push((operation, count));
        }
    }
    let mut renames = HashMap::new();
    let mut families = Vec::new();
    for ((service, _), mut members) in groups {
        let total: usize = members.iter().map(|(_, count)| count).sum();
        if members.len() < MIN_FAMILY_NAMES || total > MAX_SPANS_PER_NAME * members.len() {
            continue;
        }
        members.sort_unstable();
        let prefix = members.iter().fold(members[0].0, |prefix, (name, _)| {
            common_prefix(prefix, name)
        });
        if prefix.chars().count() < MIN_PREFIX {
            continue;
        }
        let pattern = format!("{prefix}*");
        for (name, _) in &members {
            renames.insert((service, *name), pattern.clone());
        }
        // First, middle and last by name: varied data, deterministic choice.
        let examples: Vec<String> = [0, members.len() / 2, members.len() - 1]
            .iter()
            .map(|&index| members[index].0.to_owned())
            .collect();
        families.push(OperationFamily {
            service: service.to_owned(),
            pattern,
            distinct_names: members.len(),
            spans: total,
            examples,
        });
    }
    families.sort_by(|a, b| {
        b.distinct_names
            .cmp(&a.distinct_names)
            .then_with(|| (&a.service, &a.pattern).cmp(&(&b.service, &b.pattern)))
    });
    (renames, families)
}

#[cfg(test)]
mod tests {
    use super::{common_prefix, key};

    #[test]
    fn keys_cut_at_the_first_late_digit_token() {
        assert_eq!(
            key("mongodb_capacity_get_multi_number687684_2028-09-19"),
            Some("mongodb_capacity_get_multi_")
        );
        assert_eq!(
            key("mongodb_capacity_get_multi_numbera9926671eb9a3-3_2028-02-10"),
            Some("mongodb_capacity_get_multi_")
        );
        assert_eq!(key("v2/users/123"), Some("v2/users/"));
        assert_eq!(key("mongodb_capacity_get_multi_number"), None);
        assert_eq!(key("get_u123"), None);
        assert_eq!(key("s3.GetObject"), None);
    }

    #[test]
    fn common_prefix_respects_char_boundaries() {
        assert_eq!(common_prefix("number687", "numbera99"), "number");
        assert_eq!(common_prefix("ab", "abc"), "ab");
        assert_eq!(common_prefix("é1", "é2"), "é");
    }
}
