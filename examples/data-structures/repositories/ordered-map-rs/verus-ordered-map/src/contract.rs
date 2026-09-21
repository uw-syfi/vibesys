use vstd::prelude::*;
use vstd::resource::ghost_var::GhostVar;
use vstd::resource::Loc;

verus! {

#[verifier::opaque]
pub open spec fn unique_keys(entries: Seq<(u64, u64)>) -> bool {
    forall|i: int, j: int|
        #![trigger entries[i].0, entries[j].0]
        0 <= i < j < entries.len() ==> entries[i].0 != entries[j].0
}

#[verifier::opaque]
pub open spec fn sorted_keys(entries: Seq<(u64, u64)>) -> bool {
    forall|i: int|
        #![trigger entries[i]]
        0 <= i < entries.len() - 1 ==> entries[i].0 < entries[i + 1].0
}

pub open spec fn has_key(entries: Seq<(u64, u64)>, key: u64) -> bool {
    exists|i: int| 0 <= i < entries.len() && entries[i].0 == key
}

pub open spec fn inserted_at(
    entries: Seq<(u64, u64)>,
    index: int,
    key: u64,
    value: u64,
) -> Seq<(u64, u64)> {
    entries.subrange(0, index).push((key, value)) + entries.subrange(
        index,
        entries.len() as int,
    )
}

pub open spec fn is_lower_bound(entries: Seq<(u64, u64)>, key: u64, index: int) -> bool {
    &&& 0 <= index <= entries.len()
    &&& forall|j: int| #![trigger entries[j].0] 0 <= j < index ==> entries[j].0 < key
    &&& forall|j: int| #![trigger entries[j].0] index <= j < entries.len() ==> entries[j].0 >= key
}

pub open spec fn the_lower_bound(entries: Seq<(u64, u64)>, key: u64) -> int {
    choose|i: int| is_lower_bound(entries, key, i)
}

pub open spec fn predecessor_at(entries: Seq<(u64, u64)>, key: u64) -> Option<(u64, u64)> {
    let lo = the_lower_bound(entries, key);
    if lo == 0 {
        None
    } else {
        Some(entries[lo - 1])
    }
}

pub open spec fn successor_at(entries: Seq<(u64, u64)>, key: u64) -> Option<(u64, u64)> {
    let lo = the_lower_bound(entries, key);
    let idx = if lo < entries.len() && entries[lo].0 == key {
        lo + 1
    } else {
        lo
    };
    if idx < entries.len() {
        Some(entries[idx])
    } else {
        None
    }
}

pub open spec fn range_snapshot(
    entries: Seq<(u64, u64)>,
    start: u64,
    end: u64,
    max_items: int,
    lo: int,
    hi: int,
) -> (Seq<(u64, u64)>, bool) {
    if start >= end {
        (Seq::<(u64, u64)>::empty(), false)
    } else if !(0 <= lo <= hi <= entries.len()) {
        (Seq::<(u64, u64)>::empty(), false)
    } else if max_items <= 0 {
        (Seq::<(u64, u64)>::empty(), hi > lo)
    } else if hi - lo <= max_items {
        (entries.subrange(lo, hi), false)
    } else {
        (entries.subrange(lo, lo + max_items), true)
    }
}

/// The client-owned half of the abstract ordered map. Keys are unique and
/// strictly increasing.
pub tracked struct MapToken {
    pub state: GhostVar<Seq<(u64, u64)>>,
}

impl MapToken {
    pub open spec fn id(self) -> Loc {
        self.state.id()
    }

    pub open spec fn contents(self) -> Seq<(u64, u64)> {
        self.state@
    }
}

pub(crate) trait MapConstruction: Sized {
    spec fn wf(&self) -> bool;

    spec fn token_id(&self) -> Loc;

    fn create() -> (out: (Self, Tracked<MapToken>))
        ensures
            out.0.wf(),
            out.1@.id() == out.0.token_id(),
            out.1@.contents() == Seq::<(u64, u64)>::empty(),
            unique_keys(out.1@.contents()),
            sorted_keys(out.1@.contents());
}

} // verus!
