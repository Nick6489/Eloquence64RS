//! Sound contours, retaining the original Beta 2 and Beta 4 DSP verbatim.
// These modules are historical implementations. Keep their arithmetic and
// state handling intact: the wrapper selects them rather than recreating EQ.
#[path = "audio_beta2.rs"]
mod beta2;
#[path = "audio_beta4.rs"]
mod beta4;

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub enum PresenceContour {
    #[default]
    Disabled,
    // Wire value 1 retains the former checkbox's native contour selection.
    Enabled,
    Presence,
}

#[derive(Debug)]
pub struct AudioProcessor {
    contour: PresenceContour,
    sample_rate: u32,
    presence: beta2::AudioProcessor,
    smooth: beta4::AudioProcessor,
}
impl Default for AudioProcessor {
    fn default() -> Self {
        Self::new(11_025)
    }
}
impl AudioProcessor {
    pub fn new(sample_rate: u32) -> Self {
        Self {
            contour: PresenceContour::Disabled,
            sample_rate,
            presence: beta2::AudioProcessor::new(sample_rate),
            smooth: beta4::AudioProcessor::new(sample_rate),
        }
    }
    pub fn set_contour(&mut self, contour: PresenceContour) {
        if self.contour != contour {
            self.contour = contour;
            self.presence.set_contour(beta2::PresenceContour::Enabled);
            self.smooth.set_contour(beta4::PresenceContour::Enabled);
            self.reset();
        }
    }
    pub fn reset(&mut self) {
        self.presence.reset();
        self.smooth.reset();
    }
    pub fn process(&mut self, input: &[i16]) -> Vec<i16> {
        match self.contour {
            PresenceContour::Disabled => input.to_vec(),
            PresenceContour::Presence => self.presence.process(input),
            PresenceContour::Enabled if self.sample_rate == 11_025 => self.presence.process(input),
            PresenceContour::Enabled => self.smooth.process(input),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn selected_contours_match_original_betas_sample_for_sample() {
        let input: Vec<i16> = (0..8192)
            .map(|i| ((i * 7919 % 65536) - 32768) as i16)
            .collect();
        for rate in [11_025, 16_000] {
            for contour in [PresenceContour::Presence, PresenceContour::Enabled] {
                let mut current = AudioProcessor::new(rate);
                current.set_contour(contour);
                let mut original2 = beta2::AudioProcessor::new(rate);
                original2.set_contour(beta2::PresenceContour::Enabled);
                let mut original4 = beta4::AudioProcessor::new(rate);
                original4.set_contour(beta4::PresenceContour::Enabled);
                for chunk in input.chunks(137) {
                    let expected = if contour == PresenceContour::Presence || rate == 11_025 {
                        original2.process(chunk)
                    } else {
                        original4.process(chunk)
                    };
                    assert_eq!(current.process(chunk), expected);
                }
                current.reset();
                original2.reset();
                original4.reset();
                let expected = if contour == PresenceContour::Presence || rate == 11_025 {
                    original2.process(&input)
                } else {
                    original4.process(&input)
                };
                assert_eq!(current.process(&input), expected);
            }
        }
    }
    #[test]
    fn raw_is_exact_and_switching_contours_resets_history() {
        for rate in [11_025, 16_000] {
            let input = [i16::MIN, -1, 0, 1, i16::MAX];
            let mut processor = AudioProcessor::new(rate);
            assert_eq!(processor.process(&input), input);
            processor.set_contour(PresenceContour::Presence);
            processor.process(&input);
            processor.set_contour(PresenceContour::Enabled);
            let mut fresh = AudioProcessor::new(rate);
            fresh.set_contour(PresenceContour::Enabled);
            assert_eq!(processor.process(&input), fresh.process(&input));
        }
    }
}
