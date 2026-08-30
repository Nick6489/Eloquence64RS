//! Rate-aware Presence processing for Eloquence PCM.

use std::f32::consts::PI;

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub enum PresenceContour {
    #[default]
    Disabled,
    Enabled,
}

#[derive(Clone, Copy, Debug)]
struct Coefficients {
    b0: f32,
    b1: f32,
    b2: f32,
    a1: f32,
    a2: f32,
}

impl Coefficients {
    const IDENTITY: Self = Self {
        b0: 1.0,
        b1: 0.0,
        b2: 0.0,
        a1: 0.0,
        a2: 0.0,
    };
}

#[derive(Debug)]
struct Biquad {
    coefficients: Coefficients,
    x1: f32,
    x2: f32,
    y1: f32,
    y2: f32,
}

impl Biquad {
    fn new(coefficients: Coefficients) -> Self {
        Self {
            coefficients,
            x1: 0.0,
            x2: 0.0,
            y1: 0.0,
            y2: 0.0,
        }
    }

    fn process(&mut self, sample: f32) -> f32 {
        let c = self.coefficients;
        let output =
            c.b0 * sample + c.b1 * self.x1 + c.b2 * self.x2 - c.a1 * self.y1 - c.a2 * self.y2;
        self.x2 = self.x1;
        self.x1 = sample;
        self.y2 = self.y1;
        self.y1 = output;
        output
    }

    fn reset(&mut self) {
        self.x1 = 0.0;
        self.x2 = 0.0;
        self.y1 = 0.0;
        self.y2 = 0.0;
    }
}

/// Applies the same acoustic contour at either native engine sample rate.
/// Classic 11.025 kHz Presence retains the established 2x reconstruction;
/// native 16 kHz Presence shapes the genuine engine output without resampling.
#[derive(Debug)]
pub struct AudioProcessor {
    contour: PresenceContour,
    shelf: Biquad,
    body: Biquad,
    rate_compensation: Biquad,
    sample_rate: u32,
    history: [f32; Self::HISTORY_LENGTH],
}

impl Default for AudioProcessor {
    fn default() -> Self {
        Self::new(11_025)
    }
}

impl AudioProcessor {
    const HISTORY_LENGTH: usize = 16;
    const OUTPUT_GAIN: f32 = 0.80;
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

    // Preserve the established 11.025 kHz contour coefficients literally.
    // Runtime generation is used only for native 16 kHz, where the same
    // acoustic frequencies need different digital coefficients.
    const CLASSIC_SHELF: Coefficients = Coefficients {
        b0: 1.366_705_5,
        b1: -0.460_215_84,
        b2: 0.261_982_5,
        a1: -0.003_102_395,
        a2: 0.171_574_58,
    };
    const CLASSIC_BODY: Coefficients = Coefficients {
        b0: 1.014_337_4,
        b1: -0.971_640_8,
        b2: 0.377_226_1,
        a1: -0.971_640_8,
        a2: 0.391_563_48,
    };

    pub fn new(sample_rate: u32) -> Self {
        let (shelf, body, rate_compensation) = if sample_rate == 11_025 {
            (
                Self::CLASSIC_SHELF,
                Self::CLASSIC_BODY,
                // Apply the end-to-end capture correction after 2x
                // interpolation, at the actual 22.05 kHz output rate. The
                // first +3.4 dB audition overshot the intended broad-band
                // response by about 1 dB, so retain only a gentle lift.
                high_shelf(22_050.0, 6_300.0, 1.0),
            )
        } else {
            (
                Coefficients::IDENTITY,
                Coefficients::IDENTITY,
                // Match the preferred Pythonic reconstruction's broad tonal
                // envelope without copying its aliasing or steep bandwidth
                // collapse. The target rises smoothly to about +10 dB through
                // 4--6 kHz, then the peak naturally returns to unity at the
                // genuine 8 kHz Nyquist edge instead of sustaining a lispy
                // high-shelf boost.
                peaking_eq(sample_rate as f32, 4_800.0, 0.65, 10.0),
            )
        };
        Self {
            contour: PresenceContour::Disabled,
            shelf: Biquad::new(shelf),
            body: Biquad::new(body),
            rate_compensation: Biquad::new(rate_compensation),
            sample_rate,
            history: [0.0; Self::HISTORY_LENGTH],
        }
    }

    pub fn set_contour(&mut self, contour: PresenceContour) {
        if self.contour != contour {
            self.contour = contour;
            self.reset();
        }
    }

    pub fn reset(&mut self) {
        self.shelf.reset();
        self.body.reset();
        self.rate_compensation.reset();
        self.history.fill(0.0);
    }

    pub fn process(&mut self, input: &[i16]) -> Vec<i16> {
        if self.contour == PresenceContour::Disabled {
            return input.to_vec();
        }
        let resample = self.sample_rate == 11_025;
        let mut output = Vec::with_capacity(input.len() * if resample { 2 } else { 1 });
        for &sample in input {
            let present = self.shelf.process(f32::from(sample));
            let shaped = self.body.process(present);
            if !resample {
                output.push(Self::finish(self.rate_compensation.process(shaped)));
                continue;
            }
            self.history.copy_within(..Self::HISTORY_LENGTH - 1, 1);
            self.history[0] = shaped;
            let interpolated = self
                .history
                .iter()
                .zip(Self::HALF_SAMPLE_PHASE)
                .map(|(sample, coefficient)| sample * coefficient)
                .sum::<f32>();
            let original = self.history[Self::ORIGINAL_PHASE_INDEX] * Self::ORIGINAL_PHASE_GAIN;
            output.push(Self::finish(self.rate_compensation.process(interpolated)));
            output.push(Self::finish(self.rate_compensation.process(original)));
        }
        output
    }

    fn finish(sample: f32) -> i16 {
        (sample * Self::OUTPUT_GAIN)
            .round()
            .clamp(f32::from(i16::MIN), f32::from(i16::MAX)) as i16
    }
}

fn peaking_eq(sample_rate: f32, frequency: f32, q: f32, gain_db: f32) -> Coefficients {
    let a = 10.0_f32.powf(gain_db / 40.0);
    let omega = 2.0 * PI * frequency / sample_rate;
    let alpha = omega.sin() / (2.0 * q);
    normalize(
        1.0 + alpha * a,
        -2.0 * omega.cos(),
        1.0 - alpha * a,
        1.0 + alpha / a,
        -2.0 * omega.cos(),
        1.0 - alpha / a,
    )
}

fn high_shelf(sample_rate: f32, frequency: f32, gain_db: f32) -> Coefficients {
    let a = 10.0_f32.powf(gain_db / 40.0);
    let omega = 2.0 * PI * frequency / sample_rate;
    let cosine = omega.cos();
    let alpha = omega.sin() / 2.0 * 2.0_f32.sqrt(); // RBJ shelf slope S = 1.
    let root = 2.0 * a.sqrt() * alpha;
    normalize(
        a * ((a + 1.0) + (a - 1.0) * cosine + root),
        -2.0 * a * ((a - 1.0) + (a + 1.0) * cosine),
        a * ((a + 1.0) + (a - 1.0) * cosine - root),
        (a + 1.0) - (a - 1.0) * cosine + root,
        2.0 * ((a - 1.0) - (a + 1.0) * cosine),
        (a + 1.0) - (a - 1.0) * cosine - root,
    )
}

fn normalize(b0: f32, b1: f32, b2: f32, a0: f32, a1: f32, a2: f32) -> Coefficients {
    Coefficients {
        b0: b0 / a0,
        b1: b1 / a0,
        b2: b2 / a0,
        a1: a1 / a0,
        a2: a2 / a0,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::f32::consts::TAU;

    #[test]
    fn disabled_contour_preserves_pcm_exactly_at_both_rates() {
        let input = [i16::MIN, -1, 0, 1, i16::MAX];
        for rate in [11_025, 16_000] {
            assert_eq!(AudioProcessor::new(rate).process(&input), input);
        }
    }

    #[test]
    fn enabled_contour_resamples_only_classic_mode() {
        let mut classic = AudioProcessor::new(11_025);
        classic.set_contour(PresenceContour::Enabled);
        assert_eq!(classic.process(&[1000, 2000, 3000]).len(), 6);
        let mut native = AudioProcessor::new(16_000);
        native.set_contour(PresenceContour::Enabled);
        assert_eq!(native.process(&[1000, 2000, 3000]).len(), 3);
    }

    #[test]
    fn contour_is_invariant_across_callback_chunks() {
        let input = [1000, -2000, 4000, -8000, 16_000, 500, -250, 125];
        for rate in [11_025, 16_000] {
            let mut contiguous = AudioProcessor::new(rate);
            contiguous.set_contour(PresenceContour::Enabled);
            let expected = contiguous.process(&input);
            let mut chunked = AudioProcessor::new(rate);
            chunked.set_contour(PresenceContour::Enabled);
            let mut actual = chunked.process(&input[..3]);
            actual.extend(chunked.process(&input[3..]));
            assert_eq!(actual, expected);
        }
    }

    #[test]
    fn classic_contour_retains_the_established_presence_shape() {
        let low = processed_rms(11_025, 250.0);
        let presence = processed_rms(11_025, 4_000.0);
        let lift = 20.0 * (presence / low).log10();
        assert!((3.5..6.5).contains(&lift), "11 kHz lift was {lift:.2} dB");
    }

    #[test]
    fn native_contour_matches_the_pythonic_articulation_target() {
        let low = processed_rms(16_000, 250.0);
        let articulation = processed_rms(16_000, 4_800.0);
        let lift = 20.0 * (articulation / low).log10();
        assert!(
            (9.8..10.2).contains(&lift),
            "16 kHz articulation lift was {lift:.2} dB"
        );
    }

    #[test]
    fn classic_output_compensation_targets_only_the_upper_transition_band() {
        let low = compensation_gain_db(11_025, 1_000.0);
        let upper = compensation_gain_db(11_025, 8_000.0);
        assert!(low.abs() < 0.25, "classic low band changed by {low:.2} dB");
        assert!(
            (0.7..1.1).contains(&upper),
            "classic upper correction was {upper:.2} dB"
        );
    }

    #[test]
    fn native_compensation_is_broad_and_rate_specific() {
        let low = compensation_gain_db(16_000, 500.0);
        let centre = compensation_gain_db(16_000, 4_800.0);
        assert!(low.abs() < 0.25, "native low band changed by {low:.2} dB");
        assert!(
            (9.8..10.2).contains(&centre),
            "native clarity correction was {centre:.2} dB"
        );
    }

    #[test]
    fn classic_interpolator_rejects_the_mirrored_image_band() {
        let frequency = 4_000.0;
        let input = make_tone(11_025, frequency, 4096);
        let mut processor = AudioProcessor::new(11_025);
        processor.set_contour(PresenceContour::Enabled);
        let output = processor.process(&input);
        let settled = &output[128..];
        let wanted = tone_amplitude(settled, frequency, 22_050.0);
        let image = tone_amplitude(settled, 11_025.0 - frequency, 22_050.0);
        let rejection_db = 20.0 * (image / wanted).log10();
        assert!(
            rejection_db < -35.0,
            "image rejection was {rejection_db:.2} dB"
        );
    }

    fn processed_rms(rate: u32, frequency: f32) -> f32 {
        let input = make_tone(rate, frequency, 8192);
        let mut processor = AudioProcessor::new(rate);
        processor.set_contour(PresenceContour::Enabled);
        let output = processor.process(&input);
        (output[256..]
            .iter()
            .map(|&sample| f32::from(sample).powi(2))
            .sum::<f32>()
            / (output.len() - 256) as f32)
            .sqrt()
    }

    fn compensation_gain_db(rate: u32, frequency: f32) -> f32 {
        let filter_rate = if rate == 11_025 { 22_050 } else { rate };
        let input = make_tone(filter_rate, frequency, 8192);
        let input_rms = rms(&input[256..]);
        let mut processor = AudioProcessor::new(rate);
        let output = input
            .into_iter()
            .map(|sample| processor.rate_compensation.process(f32::from(sample)))
            .collect::<Vec<_>>();
        let output_rms = (output[256..]
            .iter()
            .map(|sample| sample.powi(2))
            .sum::<f32>()
            / (output.len() - 256) as f32)
            .sqrt();
        20.0 * (output_rms / input_rms).log10()
    }

    fn rms(samples: &[i16]) -> f32 {
        (samples
            .iter()
            .map(|&sample| f32::from(sample).powi(2))
            .sum::<f32>()
            / samples.len() as f32)
            .sqrt()
    }

    fn make_tone(rate: u32, frequency: f32, length: usize) -> Vec<i16> {
        (0..length)
            .map(|index| {
                (8000.0 * (TAU * frequency * index as f32 / rate as f32).sin()).round() as i16
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
