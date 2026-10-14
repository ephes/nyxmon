//! Content decoding against reference streams from flate2's encoders.

use std::io::Write;

use flate2::Compression;
use flate2::write::{DeflateEncoder, GzEncoder, ZlibEncoder};

use super::*;

const TEXT: &[u8] = b"{\"queue\": 3, \"state\": \"ok\", \"padding\": \"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\"}";

fn gzip(data: &[u8]) -> Vec<u8> {
    let mut encoder = GzEncoder::new(Vec::new(), Compression::default());
    encoder.write_all(data).unwrap();
    encoder.finish().unwrap()
}

fn zlib(data: &[u8]) -> Vec<u8> {
    let mut encoder = ZlibEncoder::new(Vec::new(), Compression::default());
    encoder.write_all(data).unwrap();
    encoder.finish().unwrap()
}

fn raw(data: &[u8]) -> Vec<u8> {
    let mut encoder = DeflateEncoder::new(Vec::new(), Compression::default());
    encoder.write_all(data).unwrap();
    encoder.finish().unwrap()
}

fn decode_all(encodings: &[&str], data: &[u8], chunk: usize) -> Result<Vec<u8>, DecodeError> {
    let mut decoder = BodyDecoder::new(encodings.iter().copied());
    let mut out = Vec::new();
    for piece in data.chunks(chunk) {
        out.extend(decoder.decode(piece)?);
    }
    out.extend(decoder.flush()?);
    Ok(out)
}

#[test]
fn every_encoding_decodes_in_any_chunking() {
    for chunk in [1, 2, 3, 7, 64, 4096] {
        assert_eq!(
            decode_all(&["gzip"], &gzip(TEXT), chunk).unwrap(),
            TEXT,
            "{chunk}"
        );
        assert_eq!(
            decode_all(&["deflate"], &zlib(TEXT), chunk).unwrap(),
            TEXT,
            "{chunk}"
        );
        assert_eq!(decode_all(&["identity"], TEXT, chunk).unwrap(), TEXT);
        assert_eq!(decode_all(&[], TEXT, chunk).unwrap(), TEXT);
        // `Content-Encoding: deflate, gzip`: gzip applied last, decoded first.
        let chained = gzip(&zlib(TEXT));
        assert_eq!(
            decode_all(&["deflate", "gzip"], &chained, chunk).unwrap(),
            TEXT
        );
    }
}

#[test]
fn raw_deflate_is_accepted_when_the_first_chunk_is_not_zlib() {
    assert_eq!(decode_all(&["deflate"], &raw(TEXT), 4096).unwrap(), TEXT);
    assert_eq!(decode_all(&["DEFLATE "], &raw(TEXT), 4096).unwrap(), TEXT);
}

#[test]
fn unknown_encodings_pass_through() {
    assert_eq!(decode_all(&["br"], TEXT, 5).unwrap(), TEXT);
    assert_eq!(decode_all(&["zstd", "Identity"], TEXT, 5).unwrap(), TEXT);
    // A known encoding in a chain still applies.
    assert_eq!(decode_all(&["br", "gzip"], &gzip(TEXT), 5).unwrap(), TEXT);
}

#[test]
fn truncation_is_not_an_error_and_trailing_bytes_are_ignored() {
    let full = gzip(TEXT);
    let partial = decode_all(&["gzip"], &full[..full.len() - 12], 3).unwrap();
    assert!(TEXT.starts_with(&partial));
    let mut padded = full.clone();
    padded.extend_from_slice(b"trailing garbage");
    assert_eq!(decode_all(&["gzip"], &padded, 5).unwrap(), TEXT);
    let mut zpadded = zlib(TEXT);
    zpadded.extend_from_slice(b"junk");
    assert_eq!(decode_all(&["deflate"], &zpadded, 5).unwrap(), TEXT);
    // A gzip header and one byte of a deflate block: nothing decoded yet.
    assert_eq!(
        decode_all(&["gzip"], b"\x1f\x8b\x08\x00garbage", 4096).unwrap(),
        b""
    );
}

#[test]
fn corrupt_streams_are_decoding_errors() {
    assert!(decode_all(&["gzip"], b"not gzip", 4096).is_err());
    assert!(decode_all(&["gzip"], b"\x1f\x8b\x07", 4096).is_err());
    let mut bad_crc = gzip(TEXT);
    let at = bad_crc.len() - 8;
    bad_crc[at] ^= 0xff;
    assert!(decode_all(&["gzip"], &bad_crc, 4096).is_err());
    let mut bad_size = gzip(TEXT);
    let at = bad_size.len() - 1;
    bad_size[at] ^= 0xff;
    assert!(decode_all(&["gzip"], &bad_size, 4096).is_err());
    // zlib header ok, then corrupt data in a later chunk: no raw fallback.
    let mut corrupt = zlib(TEXT);
    for byte in corrupt.iter_mut().skip(2) {
        *byte = 0xff;
    }
    assert!(decode_all(&["deflate"], &corrupt, 2).is_err());
}

#[test]
fn large_expansions_are_complete() {
    let big = vec![b'a'; 1 << 20];
    assert_eq!(decode_all(&["gzip"], &gzip(&big), 1000).unwrap(), big);
    assert_eq!(decode_all(&["deflate"], &zlib(&big), 1 << 16).unwrap(), big);
}
