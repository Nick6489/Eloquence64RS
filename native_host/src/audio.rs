//! Optional audio enhancement for Eloquence's native 11.025 kHz PCM output.

/// Output mode selected by the NVDA synth setting.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub enum AudioQuality {
    #[default]
    Standard,
    Enhanced,
}

/// Stateful, causal 2x interpolator with gentle high-frequency emphasis.
///
/// Eloquence's bundled ECI binary produces 11.025 kHz PCM even when asked for
/// its documented 22.05 kHz mode. Enhanced mode therefore creates a 22.05 kHz
/// stream locally. The state is kept across ECI callback chunks so a chunk
/// boundary cannot introduce a discontinuity.
#[derive(Debug, Default)]
pub struct AudioProcessor {
    quality: AudioQuality,
    previous_input: Option<f32>,
    previous_filter_input: f32,
}

impl AudioProcessor {
    const OUTPUT_GAIN: f32 = 0.82;
    const HIGH_FREQUENCY_EMPHASIS: f32 = 0.45;

    pub fn set_quality(&mut self, quality: AudioQuality) {
        if self.quality != quality {
            self.quality = quality;
            self.reset();
        }
    }

    pub fn reset(&mut self) {
        self.previous_input = None;
        self.previous_filter_input = 0.0;
    }

    pub fn process(&mut self, input: &[i16]) -> Vec<i16> {
        if self.quality == AudioQuality::Standard {
            return input.to_vec();
        }

        let mut output = Vec::with_capacity(input.len() * 2);
        for &sample in input {
            let current = f32::from(sample);
            let interpolated = self
                .previous_input
                .map_or(current, |previous| (previous + current) * 0.5);

            // Start from the first real sample instead of zero. This avoids a
            // synthetic high-frequency transient at the beginning of speech.
            if self.previous_input.is_none() {
                self.previous_filter_input = current;
            }
            output.push(self.enhance(interpolated));
            output.push(self.enhance(current));
            self.previous_input = Some(current);
        }
        output
    }

    fn enhance(&mut self, sample: f32) -> i16 {
        let difference = sample - self.previous_filter_input;
        self.previous_filter_input = sample;
        let emphasized = (sample + Self::HIGH_FREQUENCY_EMPHASIS * difference) * Self::OUTPUT_GAIN;
        emphasized
            .round()
            .clamp(f32::from(i16::MIN), f32::from(i16::MAX)) as i16
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn standard_mode_preserves_pcm_exactly() {
        let mut processor = AudioProcessor::default();
        let input = [i16::MIN, -1, 0, 1, i16::MAX];
        assert_eq!(processor.process(&input), input);
    }

    #[test]
    fn enhanced_mode_produces_two_samples_per_input_sample() {
        let mut processor = AudioProcessor::default();
        processor.set_quality(AudioQuality::Enhanced);
        let output = processor.process(&[1000, 2000, 3000]);
        assert_eq!(output.len(), 6);
        assert_eq!(output[0], output[1]);
    }

    #[test]
    fn enhanced_output_is_invariant_across_callback_chunks() {
        let input = [1000, -2000, 4000, -8000, 16_000];
        let mut contiguous = AudioProcessor::default();
        contiguous.set_quality(AudioQuality::Enhanced);
        let expected = contiguous.process(&input);

        let mut chunked = AudioProcessor::default();
        chunked.set_quality(AudioQuality::Enhanced);
        let mut actual = chunked.process(&input[..2]);
        actual.extend(chunked.process(&input[2..]));
        assert_eq!(actual, expected);
    }

    #[test]
    fn reset_removes_state_from_the_previous_utterance() {
        let mut reused = AudioProcessor::default();
        reused.set_quality(AudioQuality::Enhanced);
        reused.process(&[20_000, -20_000]);
        reused.reset();
        let after_reset = reused.process(&[3000, 4000]);

        let mut fresh = AudioProcessor::default();
        fresh.set_quality(AudioQuality::Enhanced);
        assert_eq!(after_reset, fresh.process(&[3000, 4000]));
    }

    #[test]
    fn enhancement_saturates_instead_of_wrapping() {
        let mut processor = AudioProcessor::default();
        processor.set_quality(AudioQuality::Enhanced);
        let output = processor.process(&[i16::MIN, i16::MAX, i16::MIN, i16::MAX]);
        assert!(output
            .iter()
            .any(|&sample| sample == i16::MIN || sample == i16::MAX));
    }
}
