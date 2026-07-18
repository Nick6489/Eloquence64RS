//! Optional audio enhancement for Eloquence's native 11.025 kHz PCM output.

/// Output mode selected by the NVDA synth setting.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub enum AudioQuality {
    #[default]
    Standard,
    Enhanced,
}

/// Stateful 2x speech-band reconstruction for Eloquence PCM.
///
/// Eloquence's bundled ECI binary produces 11.025 kHz PCM even when asked for
/// its documented 22.05 kHz mode. Enhanced mode reconstructs a 22.05 kHz
/// stream locally in three deliberately separate stages:
///
/// 1. gentle body and presence shaping restores vocal weight and articulation;
/// 2. a 31-tap polyphase low-pass interpolator inserts the new samples while
///    rejecting the mirrored 7--11 kHz image band;
/// 3. conservative output gain and saturation preserve headroom.
///
/// All filter state survives ECI callback boundaries. The interpolator adds
/// about 0.7 ms of fixed latency, which is short enough to be immaterial to
/// NVDA while avoiding look-ahead and callback-boundary discontinuities.
#[derive(Debug)]
pub struct AudioProcessor {
    quality: AudioQuality,
    shelf_x1: f32,
    shelf_x2: f32,
    shelf_y1: f32,
    shelf_y2: f32,
    body_x1: f32,
    body_x2: f32,
    body_y1: f32,
    body_y2: f32,
    history: [f32; Self::HISTORY_LENGTH],
}

impl Default for AudioProcessor {
    fn default() -> Self {
        Self {
            quality: AudioQuality::Standard,
            shelf_x1: 0.0,
            shelf_x2: 0.0,
            shelf_y1: 0.0,
            shelf_y2: 0.0,
            body_x1: 0.0,
            body_x2: 0.0,
            body_y1: 0.0,
            body_y2: 0.0,
            history: [0.0; Self::HISTORY_LENGTH],
        }
    }
}

impl AudioProcessor {
    const HISTORY_LENGTH: usize = 16;
    const OUTPUT_GAIN: f32 = 0.80;

    // A 5 dB high shelf centred at 2.5 kHz, calculated for 11.025 kHz input.
    // The shelf is intentionally applied before interpolation so the image
    // rejection filter removes mirrored energy produced by the native stream.
    const SHELF_B0: f32 = 1.366_705_5;
    const SHELF_B1: f32 = -0.460_215_84;
    const SHELF_B2: f32 = 0.261_982_5;
    const SHELF_A1: f32 = -0.003_102_395;
    const SHELF_A2: f32 = 0.171_574_58;

    // A broad 0.4 dB peaking EQ centred at 1.4 kHz (Q 0.8), calculated for
    // 11.025 kHz input. This restores a small amount of vocal body without
    // changing bass or the upper-presence and image-rejection response.
    const BODY_B0: f32 = 1.014_337_4;
    const BODY_B1: f32 = -0.971_640_8;
    const BODY_B2: f32 = 0.377_226_1;
    const BODY_A1: f32 = -0.971_640_8;
    const BODY_A2: f32 = 0.391_563_48;

    // Even polyphase arm of a 31-tap Hann-windowed sinc interpolator. The odd
    // arm is a delayed original sample. Both arms have unity DC gain.
    const HALF_SAMPLE_PHASE: [f32; Self::HISTORY_LENGTH] = [
        0.0,
        0.002_116_937_3,
        -0.009_574_75,
        0.024_439_279,
        -0.050_227_597,
        0.095_495_91,
        -0.191_948_6,
        0.629_683_4,
        0.629_683_4,
        -0.191_948_6,
        0.095_495_91,
        -0.050_227_597,
        0.024_439_279,
        -0.009_574_75,
        0.002_116_937_3,
        0.0,
    ];
    const ORIGINAL_PHASE_INDEX: usize = 7;
    const ORIGINAL_PHASE_GAIN: f32 = 1.000_030_9;

    pub fn set_quality(&mut self, quality: AudioQuality) {
        if self.quality != quality {
            self.quality = quality;
            self.reset();
        }
    }

    pub fn reset(&mut self) {
        self.shelf_x1 = 0.0;
        self.shelf_x2 = 0.0;
        self.shelf_y1 = 0.0;
        self.shelf_y2 = 0.0;
        self.body_x1 = 0.0;
        self.body_x2 = 0.0;
        self.body_y1 = 0.0;
        self.body_y2 = 0.0;
        self.history.fill(0.0);
    }

    pub fn process(&mut self, input: &[i16]) -> Vec<i16> {
        if self.quality == AudioQuality::Standard {
            return input.to_vec();
        }

        let mut output = Vec::with_capacity(input.len() * 2);
        for &sample in input {
            let present = self.shape_presence(f32::from(sample));
            let shaped = self.shape_body(present);
            self.history.copy_within(..Self::HISTORY_LENGTH - 1, 1);
            self.history[0] = shaped;

            let interpolated = self
                .history
                .iter()
                .zip(Self::HALF_SAMPLE_PHASE)
                .map(|(sample, coefficient)| sample * coefficient)
                .sum::<f32>();
            let original = self.history[Self::ORIGINAL_PHASE_INDEX] * Self::ORIGINAL_PHASE_GAIN;

            output.push(Self::finish(interpolated));
            output.push(Self::finish(original));
        }
        output
    }

    fn shape_presence(&mut self, sample: f32) -> f32 {
        let output = Self::SHELF_B0 * sample
            + Self::SHELF_B1 * self.shelf_x1
            + Self::SHELF_B2 * self.shelf_x2
            - Self::SHELF_A1 * self.shelf_y1
            - Self::SHELF_A2 * self.shelf_y2;
        self.shelf_x2 = self.shelf_x1;
        self.shelf_x1 = sample;
        self.shelf_y2 = self.shelf_y1;
        self.shelf_y1 = output;
        output
    }

    fn shape_body(&mut self, sample: f32) -> f32 {
        let output =
            Self::BODY_B0 * sample + Self::BODY_B1 * self.body_x1 + Self::BODY_B2 * self.body_x2
                - Self::BODY_A1 * self.body_y1
                - Self::BODY_A2 * self.body_y2;
        self.body_x2 = self.body_x1;
        self.body_x1 = sample;
        self.body_y2 = self.body_y1;
        self.body_y1 = output;
        output
    }

    fn finish(sample: f32) -> i16 {
        (sample * Self::OUTPUT_GAIN)
            .round()
            .clamp(f32::from(i16::MIN), f32::from(i16::MAX)) as i16
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::f32::consts::TAU;

    const INPUT_RATE: f32 = 11_025.0;
    const OUTPUT_RATE: f32 = INPUT_RATE * 2.0;

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
        assert_eq!(processor.process(&[1000, 2000, 3000]).len(), 6);
    }

    #[test]
    fn enhanced_output_is_invariant_across_callback_chunks() {
        let input = [1000, -2000, 4000, -8000, 16_000, 500, -250, 125];
        let mut contiguous = AudioProcessor::default();
        contiguous.set_quality(AudioQuality::Enhanced);
        let expected = contiguous.process(&input);

        let mut chunked = AudioProcessor::default();
        chunked.set_quality(AudioQuality::Enhanced);
        let mut actual = chunked.process(&input[..3]);
        actual.extend(chunked.process(&input[3..6]));
        actual.extend(chunked.process(&input[6..]));
        assert_eq!(actual, expected);
    }

    #[test]
    fn reset_removes_state_from_the_previous_utterance() {
        let mut reused = AudioProcessor::default();
        reused.set_quality(AudioQuality::Enhanced);
        reused.process(&[20_000, -20_000, 10_000, -5000]);
        reused.reset();
        let after_reset = reused.process(&[3000, 4000, 5000]);

        let mut fresh = AudioProcessor::default();
        fresh.set_quality(AudioQuality::Enhanced);
        assert_eq!(after_reset, fresh.process(&[3000, 4000, 5000]));
    }

    #[test]
    fn silence_remains_silent() {
        let mut processor = AudioProcessor::default();
        processor.set_quality(AudioQuality::Enhanced);
        assert!(processor
            .process(&[0; 64])
            .iter()
            .all(|&sample| sample == 0));
    }

    #[test]
    fn enhancement_saturates_instead_of_wrapping() {
        let mut processor = AudioProcessor::default();
        processor.set_quality(AudioQuality::Enhanced);
        let input = (0..128)
            .map(|index| if index % 2 == 0 { i16::MIN } else { i16::MAX })
            .collect::<Vec<_>>();
        let output = processor.process(&input);
        assert!(output
            .iter()
            .any(|&sample| sample == i16::MIN || sample == i16::MAX));
    }

    #[test]
    fn presence_shelf_favours_articulation_band() {
        let low = processed_tone_rms(250.0);
        let presence = processed_tone_rms(4000.0);
        let relative_db = 20.0 * (presence / low).log10();
        assert!(
            relative_db > 3.5,
            "presence lift was only {relative_db:.2} dB"
        );
        assert!(relative_db < 6.0, "presence lift was {relative_db:.2} dB");
    }

    #[test]
    fn body_eq_gently_lifts_the_vocal_body_band() {
        let centre_db = body_eq_gain_db(1400.0);
        let low_db = body_eq_gain_db(250.0);
        let high_db = body_eq_gain_db(4000.0);

        assert!(
            (0.35..=0.45).contains(&centre_db),
            "body lift at 1.4 kHz was {centre_db:.2} dB"
        );
        assert!(low_db.abs() < 0.10, "bass changed by {low_db:.2} dB");
        assert!(
            high_db.abs() < 0.10,
            "upper presence changed by {high_db:.2} dB"
        );
    }

    #[test]
    fn interpolator_rejects_the_mirrored_image_band() {
        let frequency = 4000.0;
        let input = make_tone(frequency, 4096);
        let mut processor = AudioProcessor::default();
        processor.set_quality(AudioQuality::Enhanced);
        let output = processor.process(&input);
        let settled = &output[128..];
        let wanted = tone_amplitude(settled, frequency, OUTPUT_RATE);
        let image = tone_amplitude(settled, INPUT_RATE - frequency, OUTPUT_RATE);
        let rejection_db = 20.0 * (image / wanted).log10();
        assert!(
            rejection_db < -35.0,
            "image rejection was {rejection_db:.2} dB"
        );
    }

    fn processed_tone_rms(frequency: f32) -> f32 {
        let input = make_tone(frequency, 4096);
        let mut processor = AudioProcessor::default();
        processor.set_quality(AudioQuality::Enhanced);
        let output = processor.process(&input);
        let settled = &output[128..];
        (settled
            .iter()
            .map(|&sample| f32::from(sample).powi(2))
            .sum::<f32>()
            / settled.len() as f32)
            .sqrt()
    }

    fn body_eq_gain_db(frequency: f32) -> f32 {
        let input = make_tone(frequency, 4096);
        let input_rms = (input[128..]
            .iter()
            .map(|&sample| f32::from(sample).powi(2))
            .sum::<f32>()
            / (input.len() - 128) as f32)
            .sqrt();
        let mut processor = AudioProcessor::default();
        let output = input
            .into_iter()
            .map(|sample| processor.shape_body(f32::from(sample)))
            .collect::<Vec<_>>();
        let output_rms = (output[128..]
            .iter()
            .map(|sample| sample.powi(2))
            .sum::<f32>()
            / (output.len() - 128) as f32)
            .sqrt();
        20.0 * (output_rms / input_rms).log10()
    }

    fn make_tone(frequency: f32, length: usize) -> Vec<i16> {
        (0..length)
            .map(|index| {
                (8000.0 * (TAU * frequency * index as f32 / INPUT_RATE).sin()).round() as i16
            })
            .collect()
    }

    fn tone_amplitude(samples: &[i16], frequency: f32, sample_rate: f32) -> f32 {
        let (real, imaginary) =
            samples
                .iter()
                .enumerate()
                .fold((0.0, 0.0), |(real, imaginary), (index, &sample)| {
                    let phase = TAU * frequency * index as f32 / sample_rate;
                    (
                        real + f32::from(sample) * phase.cos(),
                        imaginary - f32::from(sample) * phase.sin(),
                    )
                });
        real.hypot(imaginary) * 2.0 / samples.len() as f32
    }
}
