//! Typed payloads carried by the versioned framing protocol.

use crate::engine::EngineEvent;
use crate::protocol::{
    Frame, MessageKind, PayloadReader, PayloadWriter, ProtocolError, AUTH_KEY_LEN,
};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct InitializeConfig {
    pub eci_path: String,
    pub data_directory: String,
    pub language_code: String,
    pub language_id: i32,
    pub enable_abbreviation_dictionary: bool,
    pub enable_phrase_prediction: bool,
    pub voice_variant: i32,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ClientCommand {
    Hello([u8; AUTH_KEY_LEN]),
    Initialize(InitializeConfig),
    BeginGeneration(u64),
    AddText(Vec<u8>),
    InsertIndex(u32),
    Synthesize,
    Stop,
    Delete,
    SetParam { parameter: i32, value: i32 },
    SetVoiceParam { parameter: i32, value: i32 },
    CopyVoice(i32),
}

impl ClientCommand {
    pub fn decode(frame: &Frame) -> Result<Self, ProtocolError> {
        let mut payload = PayloadReader::new(&frame.payload);
        let command = match frame.kind {
            MessageKind::Hello => {
                let bytes = payload.get_bytes()?;
                let key = bytes
                    .try_into()
                    .map_err(|_| ProtocolError::InvalidAuthenticationKeyLength(bytes.len()))?;
                Self::Hello(key)
            }
            MessageKind::Initialize => Self::Initialize(InitializeConfig {
                eci_path: payload.get_string()?.to_owned(),
                data_directory: payload.get_string()?.to_owned(),
                language_code: payload.get_string()?.to_owned(),
                language_id: payload.get_i32()?,
                enable_abbreviation_dictionary: payload.get_u8()? != 0,
                enable_phrase_prediction: payload.get_u8()? != 0,
                voice_variant: payload.get_i32()?,
            }),
            MessageKind::BeginGeneration => Self::BeginGeneration(payload.get_u64()?),
            MessageKind::AddText => Self::AddText(payload.get_bytes()?.to_vec()),
            MessageKind::InsertIndex => Self::InsertIndex(payload.get_u32()?),
            MessageKind::Synthesize => Self::Synthesize,
            MessageKind::Stop => Self::Stop,
            MessageKind::Delete => Self::Delete,
            MessageKind::SetParam => Self::SetParam {
                parameter: payload.get_i32()?,
                value: payload.get_i32()?,
            },
            MessageKind::SetVoiceParam => Self::SetVoiceParam {
                parameter: payload.get_i32()?,
                value: payload.get_i32()?,
            },
            MessageKind::CopyVoice => Self::CopyVoice(payload.get_i32()?),
            kind => return Err(ProtocolError::UnexpectedMessageKind(kind as u16)),
        };
        payload.finish()?;
        Ok(command)
    }
}

pub fn response(request_id: u32) -> Frame {
    Frame::new(MessageKind::Response, request_id, Vec::new())
}

pub fn error_response(request_id: u32, message: &str) -> Result<Frame, ProtocolError> {
    let mut payload = PayloadWriter::new();
    payload.put_string(message)?;
    Ok(Frame::new(
        MessageKind::ErrorResponse,
        request_id,
        payload.finish(),
    ))
}

pub fn engine_event(event: EngineEvent) -> Result<Frame, ProtocolError> {
    let mut payload = PayloadWriter::new();
    let kind = match event {
        EngineEvent::Audio {
            generation,
            samples,
        } => {
            payload.put_u64(generation);
            let mut pcm = Vec::with_capacity(samples.len() * 2);
            for sample in samples {
                pcm.extend_from_slice(&sample.to_le_bytes());
            }
            payload.put_bytes(&pcm)?;
            MessageKind::Audio
        }
        EngineEvent::Index {
            generation,
            value,
            recovered,
        } => {
            payload.put_u64(generation);
            payload.put_u32(value);
            payload.put_u8(u8::from(recovered));
            MessageKind::Index
        }
        EngineEvent::Done { generation } => {
            payload.put_u64(generation);
            MessageKind::Done
        }
        EngineEvent::Stopped { generation } => {
            payload.put_u64(generation);
            MessageKind::Stopped
        }
        EngineEvent::CallbackError(message) => {
            payload.put_string(&message)?;
            MessageKind::ErrorResponse
        }
    };
    Ok(Frame::new(kind, 0, payload.finish()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn initialize_payload_decodes_all_host_configuration() {
        let mut payload = PayloadWriter::new();
        payload.put_string(r"C:\Eloquence\ECI.DLL").unwrap();
        payload.put_string(r"C:\Eloquence").unwrap();
        payload.put_string("enu").unwrap();
        payload.put_i32(65_536);
        payload.put_u8(1);
        payload.put_u8(0);
        payload.put_i32(3);
        let frame = Frame::new(MessageKind::Initialize, 4, payload.finish());

        assert_eq!(
            ClientCommand::decode(&frame).unwrap(),
            ClientCommand::Initialize(InitializeConfig {
                eci_path: r"C:\Eloquence\ECI.DLL".to_owned(),
                data_directory: r"C:\Eloquence".to_owned(),
                language_code: "enu".to_owned(),
                language_id: 65_536,
                enable_abbreviation_dictionary: true,
                enable_phrase_prediction: false,
                voice_variant: 3,
            })
        );
    }

    #[test]
    fn audio_event_is_little_endian_pcm_with_generation() {
        let frame = engine_event(EngineEvent::Audio {
            generation: 9,
            samples: vec![0x1234, -2],
        })
        .unwrap();
        let mut payload = PayloadReader::new(&frame.payload);
        assert_eq!(frame.kind, MessageKind::Audio);
        assert_eq!(payload.get_u64().unwrap(), 9);
        assert_eq!(payload.get_bytes().unwrap(), &[0x34, 0x12, 0xfe, 0xff]);
        payload.finish().unwrap();
    }

    #[test]
    fn command_rejects_trailing_payload_data() {
        let frame = Frame::new(MessageKind::Stop, 2, vec![1]);
        assert_eq!(
            ClientCommand::decode(&frame),
            Err(ProtocolError::TrailingPayload(1))
        );
    }
}
