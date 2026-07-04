//! Synthesis progress tracking independent of the ECI and transport layers.
//!
//! Eloquence can complete synthesis without calling back the last index it was
//! given.  NVDA's Say All depends on that index to advance, so completion must
//! recover the newest pending index before reporting `Done`.

pub const FINAL_INDEX: u32 = 0xffff;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ProgressEvent {
    Index {
        generation: u64,
        value: u32,
        recovered: bool,
    },
    Done {
        generation: u64,
    },
    Stopped {
        generation: u64,
    },
}

#[derive(Debug, Default)]
pub struct ProgressTracker {
    generation: Option<u64>,
    pending_indexes: Vec<u32>,
    done: bool,
}

impl ProgressTracker {
    pub fn begin(&mut self, generation: u64) {
        self.generation = Some(generation);
        self.pending_indexes.clear();
        self.done = false;
    }

    pub fn insert_index(&mut self, index: u32) {
        if self.generation.is_some() && !self.done && index != FINAL_INDEX {
            self.pending_indexes.push(index);
        }
    }

    pub fn current_generation(&self) -> Option<u64> {
        self.active_generation()
    }

    /// Records a normal ECI index callback.
    ///
    /// A later observed index proves that preceding indexes were crossed too,
    /// so all pending entries through it are discarded just as NVDA's speech
    /// manager advances to the newest reported index.
    pub fn engine_index(&mut self, index: u32) -> Option<ProgressEvent> {
        let generation = self.active_generation()?;
        if index == FINAL_INDEX {
            return None;
        }

        if let Some(position) = self
            .pending_indexes
            .iter()
            .position(|value| *value == index)
        {
            self.pending_indexes.drain(..=position);
        }

        Some(ProgressEvent::Index {
            generation,
            value: index,
            recovered: false,
        })
    }

    /// Completes the current generation, recovering a swallowed trailing index.
    ///
    /// This method is idempotent because ECI may send its final-index callback
    /// before `eciSynchronize` returns.  Both paths are allowed to call it.
    pub fn complete(&mut self) -> Vec<ProgressEvent> {
        let Some(generation) = self.active_generation() else {
            return Vec::new();
        };

        self.done = true;
        let mut events = Vec::with_capacity(2);
        if let Some(index) = self.pending_indexes.pop() {
            events.push(ProgressEvent::Index {
                generation,
                value: index,
                recovered: true,
            });
        }
        self.pending_indexes.clear();
        events.push(ProgressEvent::Done { generation });
        events
    }

    pub fn stop(&mut self) -> Option<ProgressEvent> {
        let generation = self.generation.take()?;
        self.pending_indexes.clear();
        self.done = true;
        Some(ProgressEvent::Stopped { generation })
    }

    fn active_generation(&self) -> Option<u64> {
        self.generation.filter(|_| !self.done)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn normal_indexes_are_reported_without_recovery() {
        let mut tracker = ProgressTracker::default();
        tracker.begin(7);
        tracker.insert_index(10);
        tracker.insert_index(20);

        assert_eq!(
            tracker.engine_index(10),
            Some(ProgressEvent::Index {
                generation: 7,
                value: 10,
                recovered: false,
            })
        );
        assert_eq!(
            tracker.engine_index(20),
            Some(ProgressEvent::Index {
                generation: 7,
                value: 20,
                recovered: false,
            })
        );
        assert_eq!(
            tracker.complete(),
            vec![ProgressEvent::Done { generation: 7 }]
        );
    }

    #[test]
    fn completion_recovers_the_latest_swallowed_index_before_done() {
        let mut tracker = ProgressTracker::default();
        tracker.begin(111);
        tracker.insert_index(5543);
        tracker.insert_index(5544);
        tracker.engine_index(5543);

        assert_eq!(
            tracker.complete(),
            vec![
                ProgressEvent::Index {
                    generation: 111,
                    value: 5544,
                    recovered: true,
                },
                ProgressEvent::Done { generation: 111 },
            ]
        );
    }

    #[test]
    fn observing_a_later_index_discards_all_earlier_pending_indexes() {
        let mut tracker = ProgressTracker::default();
        tracker.begin(9);
        tracker.insert_index(100);
        tracker.insert_index(200);
        tracker.insert_index(300);
        tracker.engine_index(200);

        assert_eq!(
            tracker.complete(),
            vec![
                ProgressEvent::Index {
                    generation: 9,
                    value: 300,
                    recovered: true,
                },
                ProgressEvent::Done { generation: 9 },
            ]
        );
    }

    #[test]
    fn completion_is_idempotent_across_callback_and_synchronize_paths() {
        let mut tracker = ProgressTracker::default();
        tracker.begin(3);

        assert_eq!(
            tracker.complete(),
            vec![ProgressEvent::Done { generation: 3 }]
        );
        assert!(tracker.complete().is_empty());
    }

    #[test]
    fn stop_invalidates_pending_progress_and_old_callbacks() {
        let mut tracker = ProgressTracker::default();
        tracker.begin(5);
        tracker.insert_index(99);

        assert_eq!(
            tracker.stop(),
            Some(ProgressEvent::Stopped { generation: 5 })
        );
        assert_eq!(tracker.engine_index(99), None);
        assert!(tracker.complete().is_empty());
    }

    #[test]
    fn a_new_generation_cannot_inherit_old_pending_indexes() {
        let mut tracker = ProgressTracker::default();
        tracker.begin(1);
        tracker.insert_index(10);
        tracker.begin(2);

        assert_eq!(
            tracker.complete(),
            vec![ProgressEvent::Done { generation: 2 }]
        );
    }
}
