use vstd::prelude::*;
use vstd::resource::ghost_var::GhostVar;
use vstd::resource::Loc;

verus! {

pub open spec fn unique_keys(entries: Seq<(u64, u64)>) -> bool {
    forall|i: int, j: int|
        0 <= i < j < entries.len() ==> entries[i].0 != entries[j].0
}

pub open spec fn has_key(entries: Seq<(u64, u64)>, key: u64) -> bool {
    exists|i: int| 0 <= i < entries.len() && entries[i].0 == key
}

/// The client-owned half of the abstract unordered map. Keys in `contents`
/// are unique; order is not part of the specification.
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

/// Fixed construction contract for a candidate-owned concurrent map.
pub(crate) trait MapConstruction: Sized {
    spec fn wf(&self) -> bool;

    spec fn token_id(&self) -> Loc;

    fn create() -> (out: (Self, Tracked<MapToken>))
        ensures
            out.0.wf(),
            out.1@.id() == out.0.token_id(),
            out.1@.contents() == Seq::<(u64, u64)>::empty();
}

} // verus!
