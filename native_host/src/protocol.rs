use std::error::Error;
use std::fmt;

pub const MAGIC: [u8; 4] = *b"ELQH";
pub const VERSION: u16 = 1;
pub const HEADER_LEN: usize = 20;
pub const MAX_PAYLOAD_LEN: usize = 4 * 1024 * 1024;
pub const AUTH_KEY_LEN: usize = 16;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[repr(u16)]
pub enum MessageKind {
    Hello = 0x0001,
    Initialize = 0x0010,
    BeginGeneration = 0x0011,
    AddText = 0x0012,
    InsertIndex = 0x0013,
    Synthesize = 0x0014,
    Stop = 0x0015,
    Delete = 0x0016,
    SetParam = 0x0020,
    SetVoiceParam = 0x0021,
    CopyVoice = 0x0022,
    HelloAck = 0x8001,
    Response = 0x8002,
    ErrorResponse = 0x8003,
    Audio = 0x9000,
    Index = 0x9001,
    Done = 0x9002,
    Stopped = 0x9003,
}

impl TryFrom<u16> for MessageKind {
    type Error = ProtocolError;

    fn try_from(value: u16) -> Result<Self, Self::Error> {
        let kind = match value {
            0x0001 => Self::Hello,
            0x0010 => Self::Initialize,
            0x0011 => Self::BeginGeneration,
            0x0012 => Self::AddText,
            0x0013 => Self::InsertIndex,
            0x0014 => Self::Synthesize,
            0x0015 => Self::Stop,
            0x0016 => Self::Delete,
            0x0020 => Self::SetParam,
            0x0021 => Self::SetVoiceParam,
            0x0022 => Self::CopyVoice,
            0x8001 => Self::HelloAck,
            0x8002 => Self::Response,
            0x8003 => Self::ErrorResponse,
            0x9000 => Self::Audio,
            0x9001 => Self::Index,
            0x9002 => Self::Done,
            0x9003 => Self::Stopped,
            _ => return Err(ProtocolError::UnknownMessageKind(value)),
        };
        Ok(kind)
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Frame {
    pub kind: MessageKind,
    pub request_id: u32,
    pub flags: u32,
    pub payload: Vec<u8>,
}

impl Frame {
    pub fn new(kind: MessageKind, request_id: u32, payload: Vec<u8>) -> Self {
        Self {
            kind,
            request_id,
            flags: 0,
            payload,
        }
    }

    pub fn encode(&self) -> Result<Vec<u8>, ProtocolError> {
        if self.payload.len() > MAX_PAYLOAD_LEN {
            return Err(ProtocolError::PayloadTooLarge(self.payload.len()));
        }
        let mut encoded = Vec::with_capacity(HEADER_LEN + self.payload.len());
        encoded.extend_from_slice(&MAGIC);
        encoded.extend_from_slice(&VERSION.to_le_bytes());
        encoded.extend_from_slice(&(self.kind as u16).to_le_bytes());
        encoded.extend_from_slice(&self.request_id.to_le_bytes());
        encoded.extend_from_slice(&self.flags.to_le_bytes());
        encoded.extend_from_slice(&(self.payload.len() as u32).to_le_bytes());
        encoded.extend_from_slice(&self.payload);
        Ok(encoded)
    }

    pub fn decode(encoded: &[u8]) -> Result<Self, ProtocolError> {
        if encoded.len() < HEADER_LEN {
            return Err(ProtocolError::TruncatedHeader(encoded.len()));
        }
        if encoded[..4] != MAGIC {
            return Err(ProtocolError::InvalidMagic(
                encoded[..4].try_into().unwrap(),
            ));
        }
        let version = u16::from_le_bytes(encoded[4..6].try_into().unwrap());
        if version != VERSION {
            return Err(ProtocolError::UnsupportedVersion(version));
        }
        let kind = MessageKind::try_from(u16::from_le_bytes(encoded[6..8].try_into().unwrap()))?;
        let request_id = u32::from_le_bytes(encoded[8..12].try_into().unwrap());
        let flags = u32::from_le_bytes(encoded[12..16].try_into().unwrap());
        let payload_len = u32::from_le_bytes(encoded[16..20].try_into().unwrap()) as usize;
        if payload_len > MAX_PAYLOAD_LEN {
            return Err(ProtocolError::PayloadTooLarge(payload_len));
        }
        let actual_payload_len = encoded.len() - HEADER_LEN;
        if actual_payload_len != payload_len {
            return Err(ProtocolError::PayloadLengthMismatch {
                declared: payload_len,
                actual: actual_payload_len,
            });
        }
        Ok(Self {
            kind,
            request_id,
            flags,
            payload: encoded[HEADER_LEN..].to_vec(),
        })
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum ProtocolError {
    TruncatedHeader(usize),
    InvalidMagic([u8; 4]),
    UnsupportedVersion(u16),
    UnknownMessageKind(u16),
    PayloadTooLarge(usize),
    PayloadLengthMismatch { declared: usize, actual: usize },
    TruncatedPayload,
    InvalidUtf8,
}

impl fmt::Display for ProtocolError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::TruncatedHeader(length) => {
                write!(formatter, "truncated frame header: {length} bytes")
            }
            Self::InvalidMagic(magic) => write!(formatter, "invalid frame magic: {magic:?}"),
            Self::UnsupportedVersion(version) => {
                write!(formatter, "unsupported protocol version: {version}")
            }
            Self::UnknownMessageKind(kind) => {
                write!(formatter, "unknown message kind: {kind:#06x}")
            }
            Self::PayloadTooLarge(length) => {
                write!(formatter, "payload exceeds protocol limit: {length}")
            }
            Self::PayloadLengthMismatch { declared, actual } => {
                write!(
                    formatter,
                    "payload length mismatch: declared {declared}, actual {actual}"
                )
            }
            Self::TruncatedPayload => write!(formatter, "truncated typed payload"),
            Self::InvalidUtf8 => write!(formatter, "invalid UTF-8 protocol string"),
        }
    }
}

impl Error for ProtocolError {}

#[derive(Default)]
pub struct PayloadWriter {
    bytes: Vec<u8>,
}

impl PayloadWriter {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn put_u8(&mut self, value: u8) {
        self.bytes.push(value);
    }

    pub fn put_u32(&mut self, value: u32) {
        self.bytes.extend_from_slice(&value.to_le_bytes());
    }

    pub fn put_i32(&mut self, value: i32) {
        self.bytes.extend_from_slice(&value.to_le_bytes());
    }

    pub fn put_u64(&mut self, value: u64) {
        self.bytes.extend_from_slice(&value.to_le_bytes());
    }

    pub fn put_bytes(&mut self, value: &[u8]) -> Result<(), ProtocolError> {
        let length =
            u32::try_from(value.len()).map_err(|_| ProtocolError::PayloadTooLarge(value.len()))?;
        self.put_u32(length);
        self.bytes.extend_from_slice(value);
        Ok(())
    }

    pub fn put_string(&mut self, value: &str) -> Result<(), ProtocolError> {
        self.put_bytes(value.as_bytes())
    }

    pub fn finish(self) -> Vec<u8> {
        self.bytes
    }
}

pub struct PayloadReader<'a> {
    remaining: &'a [u8],
}

impl<'a> PayloadReader<'a> {
    pub fn new(payload: &'a [u8]) -> Self {
        Self { remaining: payload }
    }

    fn take(&mut self, length: usize) -> Result<&'a [u8], ProtocolError> {
        if self.remaining.len() < length {
            return Err(ProtocolError::TruncatedPayload);
        }
        let (value, remaining) = self.remaining.split_at(length);
        self.remaining = remaining;
        Ok(value)
    }

    pub fn get_u8(&mut self) -> Result<u8, ProtocolError> {
        Ok(self.take(1)?[0])
    }

    pub fn get_u32(&mut self) -> Result<u32, ProtocolError> {
        Ok(u32::from_le_bytes(self.take(4)?.try_into().unwrap()))
    }

    pub fn get_i32(&mut self) -> Result<i32, ProtocolError> {
        Ok(i32::from_le_bytes(self.take(4)?.try_into().unwrap()))
    }

    pub fn get_u64(&mut self) -> Result<u64, ProtocolError> {
        Ok(u64::from_le_bytes(self.take(8)?.try_into().unwrap()))
    }

    pub fn get_bytes(&mut self) -> Result<&'a [u8], ProtocolError> {
        let length = self.get_u32()? as usize;
        self.take(length)
    }

    pub fn get_string(&mut self) -> Result<&'a str, ProtocolError> {
        std::str::from_utf8(self.get_bytes()?).map_err(|_| ProtocolError::InvalidUtf8)
    }

    pub fn is_empty(&self) -> bool {
        self.remaining.is_empty()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn frame_encoding_has_stable_golden_bytes() {
        let frame = Frame {
            kind: MessageKind::InsertIndex,
            request_id: 0x1122_3344,
            flags: 0x5566_7788,
            payload: vec![0xaa, 0xbb],
        };
        assert_eq!(
            frame.encode().unwrap(),
            vec![
                b'E', b'L', b'Q', b'H', 1, 0, 0x13, 0, 0x44, 0x33, 0x22, 0x11, 0x88, 0x77, 0x66,
                0x55, 2, 0, 0, 0, 0xaa, 0xbb,
            ]
        );
    }

    #[test]
    fn frame_round_trip_preserves_fields() {
        let frame = Frame::new(MessageKind::Audio, 0, vec![1, 2, 3, 4]);
        assert_eq!(Frame::decode(&frame.encode().unwrap()).unwrap(), frame);
    }

    #[test]
    fn frame_rejects_declared_length_mismatch() {
        let mut encoded = Frame::new(MessageKind::Done, 0, vec![]).encode().unwrap();
        encoded[16] = 1;
        assert_eq!(
            Frame::decode(&encoded),
            Err(ProtocolError::PayloadLengthMismatch {
                declared: 1,
                actual: 0,
            })
        );
    }

    #[test]
    fn typed_payload_round_trip_preserves_engine_bytes() {
        let mut writer = PayloadWriter::new();
        writer.put_u64(42);
        writer.put_i32(-7);
        writer.put_string("enu").unwrap();
        writer.put_bytes(&[0x97, 0x00, 0xff]).unwrap();

        let payload = writer.finish();
        let mut reader = PayloadReader::new(&payload);
        assert_eq!(reader.get_u64().unwrap(), 42);
        assert_eq!(reader.get_i32().unwrap(), -7);
        assert_eq!(reader.get_string().unwrap(), "enu");
        assert_eq!(reader.get_bytes().unwrap(), &[0x97, 0x00, 0xff]);
        assert!(reader.is_empty());
    }
}
