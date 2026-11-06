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

#[test]
fn optional_gzip_headers_are_bounded_across_frames() {
    for flag in [0x08, 0x10] {
        let mut decoder = Gzip::new();
        decoder
            .decode(&[0x1f, 0x8b, 8, flag, 0, 0, 0, 0, 0, 0], usize::MAX)
            .unwrap();
        let mut rejected = false;
        for _ in 0..=128 {
            if decoder.decode(&[b'x'; 1024], usize::MAX).is_err() {
                rejected = true;
                break;
            }
        }
        assert!(
            rejected,
            "unterminated optional flag {flag} must be refused"
        );
        assert!(decoder.header.len() <= 128 * 1024);
    }
}

#[test]
fn valid_optional_gzip_headers_still_decode() {
    let mut encoder = flate2::GzBuilder::new()
        .filename("synthetic.json")
        .comment("synthetic fixture")
        .extra(vec![7; 65535])
        .write(Vec::new(), Compression::default());
    encoder.write_all(TEXT).unwrap();
    let compressed = encoder.finish().unwrap();
    for chunk in [1, 1024, compressed.len()] {
        assert_eq!(decode_all(&["gzip"], &compressed, chunk).unwrap(), TEXT);
    }
}

#[test]
fn inflaters_stop_before_exceeding_the_output_allowance() {
    let big = vec![b'a'; 1024 * 1024];
    for (is_raw, encoded) in [(false, gzip(&big)), (false, zlib(&big)), (true, raw(&big))] {
        let encoding = if encoded.starts_with(&[0x1f, 0x8b]) {
            "gzip"
        } else {
            "deflate"
        };
        for chunk_size in [1, 17, encoded.len()] {
            // Preserve httpx's first-chunk-only raw fallback: a single raw
            // byte may not reject the zlib header until a subsequent chunk.
            if is_raw && chunk_size == 1 {
                continue;
            }
            let mut decoder = BodyDecoder::with_limit([encoding], 31);
            let mut error = None;
            for chunk in encoded.chunks(chunk_size) {
                match decoder.decode(chunk) {
                    Ok(bytes) => assert!(bytes.len() <= 31),
                    Err(err) => {
                        error = Some(err);
                        break;
                    }
                }
            }
            assert_eq!(
                error,
                Some(DecodeError::TooLarge),
                "{encoding}/{chunk_size}"
            );
        }
    }
    // Inspect the inflater's own output counter: detection is inside inflation,
    // not a size check after allocating a fully expanded response.
    let mut inflater = Inflate::new(true);
    assert_eq!(
        inflater.feed(&zlib(&big), 31).unwrap_err(),
        DecodeError::TooLarge
    );
    assert!(inflater.inner.total_out() <= 32);
}

#[test]
fn each_stacked_stage_and_identity_have_cumulative_allowances() {
    let big = vec![b'a'; 1024 * 1024];
    let intermediate = zlib(&big);
    let mut decoder = BodyDecoder::with_limit(["deflate", "gzip"], intermediate.len());
    assert_eq!(
        decoder.decode(&gzip(&intermediate)).unwrap_err(),
        DecodeError::TooLarge
    );
    // Even a large intermediate that would eventually shrink is bounded.
    let mut intermediate = zlib(TEXT);
    intermediate.extend(vec![0; 4096]);
    let mut decoder = BodyDecoder::with_limit(["deflate", "gzip"], TEXT.len());
    assert_eq!(
        decoder.decode(&gzip(&intermediate)).unwrap_err(),
        DecodeError::TooLarge
    );
    for encodings in [vec![], vec!["identity"], vec!["br"]] {
        let mut decoder = BodyDecoder::with_limit(encodings, 3);
        assert_eq!(decoder.decode(b"abc").unwrap(), b"abc");
        assert_eq!(decoder.decode(b"d").unwrap_err(), DecodeError::TooLarge);
    }
}

#[test]
fn exact_output_limits_allow_trailers_and_split_frames() {
    for (encoding, encoded) in [
        ("gzip", gzip(TEXT)),
        ("deflate", zlib(TEXT)),
        ("deflate", raw(TEXT)),
    ] {
        let mut decoder = BodyDecoder::with_limit([encoding], TEXT.len());
        let mut out = Vec::new();
        // Raw deflate fallback requires enough initial bytes to reject zlib.
        for chunk in encoded.chunks(7) {
            out.extend(decoder.decode(chunk).unwrap());
        }
        out.extend(decoder.flush().unwrap());
        assert_eq!(out, TEXT);
    }
}

#[test]
fn tiny_header_frames_use_bounded_amortized_reservations() {
    let mut decoder = Gzip::new();
    decoder
        .decode(&[0x1f, 0x8b, 8, 8, 0, 0, 0, 0, 0, 0], usize::MAX)
        .unwrap();
    let mut capacity = decoder.header.capacity();
    let mut changes = 0;
    for _ in 0..4096 {
        decoder.decode(b"x", usize::MAX).unwrap();
        if decoder.header.capacity() != capacity {
            changes += 1;
            capacity = decoder.header.capacity();
        }
        assert!(capacity <= MAX_GZIP_HEADER_BYTES);
    }
    assert!(
        changes <= 12,
        "one-byte frames caused {changes} reallocations"
    );
}
