//! `Content-Encoding` decoding as httpx 0.28 does it (`httpx/_decoders.py`,
//! `Response._get_content_decoder`), without brotli or zstd (not installed
//! with the Python agent, so `br`/`zstd` pass through undecoded).
//!
//! Like Python's `zlib.decompressobj`, a truncated stream is not an error
//! (the data decoded so far is returned) and bytes after the end of a stream
//! are ignored. Corrupt data is a `DecodingError`, which httpx reports as a
//! `RequestError`.

use flate2::{Crc, Decompress, FlushDecompress, Status};

/// `httpx.DecodingError`; the message is P2 (zlib's text in Python).
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum DecodeError {
    Invalid(String),
    TooLarge,
}

impl std::fmt::Display for DecodeError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Invalid(message) => f.write_str(message),
            Self::TooLarge => f.write_str("decoded response exceeds output limit"),
        }
    }
}

impl std::error::Error for DecodeError {}

fn zlib_error(detail: &str) -> DecodeError {
    DecodeError::Invalid(format!("Error -3 while decompressing data: {detail}"))
}

/// A streaming inflater (`zlib.decompressobj` with a zlib or raw stream).
struct Inflate {
    inner: Decompress,
    done: bool,
}

impl Inflate {
    fn new(zlib_header: bool) -> Self {
        Inflate {
            inner: Decompress::new(zlib_header),
            done: false,
        }
    }

    /// Decompress `input`; returns the output and how many input bytes the
    /// stream consumed (less than `input.len()` once the stream ended).
    fn feed(&mut self, input: &[u8], allowance: usize) -> Result<(Vec<u8>, usize), DecodeError> {
        let mut out = Vec::new();
        let mut consumed = 0;
        let mut buffer = [0u8; 8192];
        while !self.done {
            let remaining = allowance - out.len();
            // Probe one byte on the stack at an exact limit, so stream endings
            // and trailers may complete without allocating beyond the allowance.
            let size = remaining.clamp(1, buffer.len());
            let before_in = self.inner.total_in();
            let before_out = self.inner.total_out();
            let status = self
                .inner
                .decompress(
                    &input[consumed..],
                    &mut buffer[..size],
                    FlushDecompress::None,
                )
                .map_err(|err| zlib_error(&err.to_string()))?;
            let read = usize::try_from(self.inner.total_in() - before_in).unwrap_or(usize::MAX);
            let wrote = usize::try_from(self.inner.total_out() - before_out).unwrap_or(usize::MAX);
            if wrote > remaining {
                return Err(DecodeError::TooLarge);
            }
            if out.capacity() - out.len() < wrote {
                let capacity = out.capacity().max(4096).saturating_mul(2).min(allowance);
                out.reserve_exact(capacity - out.len());
            }
            out.extend_from_slice(&buffer[..wrote]);
            consumed += read;
            if status == Status::StreamEnd {
                self.done = true;
            } else if wrote == size {
                continue;
            } else if consumed >= input.len() || (read == 0 && wrote == 0) {
                break;
            }
        }
        Ok((out, consumed))
    }
}

/// Maximum combined fixed/optional gzip header, independent of decoded output.
const MAX_GZIP_HEADER_BYTES: usize = 128 * 1024;

/// `zlib.decompressobj(zlib.MAX_WBITS | 16)`: one gzip member.
struct Gzip {
    header: Vec<u8>,
    header_done: bool,
    body: Option<Inflate>,
    crc: Crc,
    trailer: Vec<u8>,
    finished: bool,
}

impl Gzip {
    fn new() -> Self {
        Gzip {
            header: Vec::new(),
            header_done: false,
            body: None,
            crc: Crc::new(),
            trailer: Vec::new(),
            finished: false,
        }
    }

    /// The length of a complete gzip header at the start of `buf`, `None`
    /// while more bytes are needed.
    fn header_len(buf: &[u8]) -> Result<Option<usize>, DecodeError> {
        if buf.len() >= 2 && buf[..2] != [0x1f, 0x8b] {
            return Err(zlib_error("incorrect header check"));
        }
        if buf.len() >= 3 && buf[2] != 8 {
            return Err(zlib_error("unknown compression method"));
        }
        if buf.len() < 10 {
            return Ok(None);
        }
        let flags = buf[3];
        if flags & 0xe0 != 0 {
            return Err(zlib_error("unknown header flags set"));
        }
        let mut pos = 10;
        if flags & 0x04 != 0 {
            let Some(len) = buf.get(pos..pos + 2) else {
                return Ok(None);
            };
            pos += 2 + usize::from(u16::from_le_bytes([len[0], len[1]]));
        }
        for flag in [0x08, 0x10] {
            if flags & flag != 0 {
                match buf
                    .get(pos..)
                    .and_then(|rest| rest.iter().position(|b| *b == 0))
                {
                    Some(end) => pos += end + 1,
                    None => return Ok(None),
                }
            }
        }
        if flags & 0x02 != 0 {
            let Some(stored) = buf.get(pos..pos + 2) else {
                return Ok(None);
            };
            let mut crc = Crc::new();
            crc.update(&buf[..pos]);
            if u16::from_le_bytes([stored[0], stored[1]]) != (crc.sum() & 0xffff) as u16 {
                return Err(zlib_error("header crc mismatch"));
            }
            pos += 2;
        }
        Ok((buf.len() >= pos).then_some(pos))
    }

    fn decode(&mut self, data: &[u8], allowance: usize) -> Result<Vec<u8>, DecodeError> {
        if self.finished || data.is_empty() {
            return Ok(Vec::new());
        }
        if !self.header_done {
            // Buffer only a bounded prefix. A single frame may also contain a
            // large body; decode the remaining frame directly after the header.
            let take = data.len().min(MAX_GZIP_HEADER_BYTES - self.header.len());
            if self.header.capacity() - self.header.len() < take {
                let capacity = self
                    .header
                    .capacity()
                    .max(1024)
                    .saturating_mul(2)
                    .max(self.header.len() + take)
                    .min(MAX_GZIP_HEADER_BYTES);
                self.header.reserve_exact(capacity - self.header.len());
            }
            self.header.extend_from_slice(&data[..take]);
            let Some(len) = Self::header_len(&self.header)? else {
                if self.header.len() == MAX_GZIP_HEADER_BYTES {
                    return Err(zlib_error("gzip header exceeds 131072 byte limit"));
                }
                return Ok(Vec::new());
            };
            self.header_done = true;
            let buffered = std::mem::take(&mut self.header);
            let mut out = self.decode(&buffered[len..], allowance)?;
            let rest = self.decode(&data[take..], allowance - out.len())?;
            out.reserve_exact(rest.len());
            out.extend_from_slice(&rest);
            return Ok(out);
        }
        let mut input = data;
        let body = self.body.get_or_insert_with(|| Inflate::new(false));
        let mut out = Vec::new();
        if !body.done {
            let (decoded, consumed) = body.feed(input, allowance)?;
            self.crc.update(&decoded);
            input = &input[consumed..];
            out = decoded;
        }
        if self.body.as_ref().is_some_and(|b| b.done) {
            self.read_trailer(input)?;
        }
        Ok(out)
    }

    /// The CRC32 and ISIZE trailer; checked once complete (zlib reports a
    /// mismatch, a short trailer is a truncated stream).
    fn read_trailer(&mut self, input: &[u8]) -> Result<(), DecodeError> {
        let need = 8 - self.trailer.len();
        self.trailer
            .extend_from_slice(&input[..need.min(input.len())]);
        if self.trailer.len() < 8 {
            return Ok(());
        }
        self.finished = true;
        let crc = u32::from_le_bytes(self.trailer[..4].try_into().expect("4 bytes"));
        let size = u32::from_le_bytes(self.trailer[4..].try_into().expect("4 bytes"));
        if crc != self.crc.sum() {
            return Err(zlib_error("incorrect data check"));
        }
        if size != self.crc.amount() {
            return Err(zlib_error("incorrect length check"));
        }
        Ok(())
    }
}

/// httpx `DeflateDecoder`: a zlib stream, or a raw deflate stream when the
/// first chunk fails as zlib.
struct Deflate {
    first_attempt: bool,
    inner: Inflate,
}

impl Deflate {
    fn decode(&mut self, data: &[u8], allowance: usize) -> Result<Vec<u8>, DecodeError> {
        let was_first = std::mem::replace(&mut self.first_attempt, false);
        match self.inner.feed(data, allowance) {
            Ok((out, _)) => Ok(out),
            Err(DecodeError::Invalid(_)) if was_first => {
                self.inner = Inflate::new(false);
                self.decode(data, allowance)
            }
            Err(err) => Err(err),
        }
    }
}

enum Stage {
    Gzip(Box<Gzip>),
    Deflate(Deflate),
    Identity,
}

impl Stage {
    fn decode(&mut self, data: &[u8], allowance: usize) -> Result<Vec<u8>, DecodeError> {
        match self {
            Stage::Gzip(gzip) => gzip.decode(data, allowance),
            Stage::Deflate(deflate) => deflate.decode(data, allowance),
            Stage::Identity => {
                if data.len() > allowance {
                    return Err(DecodeError::TooLarge);
                }
                Ok(data.to_vec())
            }
        }
    }
}

/// The decoder for a response's `Content-Encoding` values.
pub struct BodyDecoder {
    /// In decoding order (the reverse of the header order).
    stages: Vec<(Stage, usize)>,
}

impl BodyDecoder {
    /// `Response._get_content_decoder` for the comma-split header values:
    /// known encodings (`identity`, `gzip`, `deflate`, case-insensitive)
    /// become decoders, unknown ones are skipped.
    pub fn new<'a>(encodings: impl IntoIterator<Item = &'a str>) -> Self {
        Self::with_limit(encodings, usize::MAX)
    }

    /// Bound cumulative output of every stage, including intermediate streams.
    pub fn with_limit<'a>(encodings: impl IntoIterator<Item = &'a str>, allowance: usize) -> Self {
        let mut stages: Vec<Stage> = encodings
            .into_iter()
            .filter_map(|value| match value.trim().to_lowercase().as_str() {
                "identity" => Some(Stage::Identity),
                "gzip" => Some(Stage::Gzip(Box::new(Gzip::new()))),
                "deflate" => Some(Stage::Deflate(Deflate {
                    first_attempt: true,
                    inner: Inflate::new(true),
                })),
                _ => None,
            })
            .collect();
        stages.reverse();
        if stages.is_empty() {
            stages.push(Stage::Identity);
        }
        BodyDecoder {
            stages: stages.into_iter().map(|stage| (stage, allowance)).collect(),
        }
    }

    /// Decode one raw chunk.
    ///
    /// # Errors
    /// Corrupt compressed data.
    pub fn decode(&mut self, data: &[u8]) -> Result<Vec<u8>, DecodeError> {
        let mut data = std::borrow::Cow::Borrowed(data);
        for (stage, remaining) in &mut self.stages {
            let decoded = stage.decode(&data, *remaining)?;
            *remaining -= decoded.len();
            data = std::borrow::Cow::Owned(decoded);
        }
        Ok(data.into_owned())
    }

    /// The end of the body. zlib's `flush()` returns nothing further for the
    /// streams decoded here (output is produced eagerly), but a chained
    /// decoder still passes the earlier stages' output through the later
    /// ones (`MultiDecoder.flush`).
    ///
    /// # Errors
    /// Corrupt compressed data.
    pub fn flush(&mut self) -> Result<Vec<u8>, DecodeError> {
        self.decode(&[])
    }
}

#[cfg(test)]
mod tests;
