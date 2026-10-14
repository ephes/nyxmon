//! The DNS wire format and text forms as dnspython 2.8 handles them for the
//! DNS check: query names from text (`dns.name.from_text`), name text with
//! dnspython's escaping (`Name.to_text`), queries (`make_query`), response
//! parsing (`dns.message.from_wire`) for the record types the check renders,
//! and the record texts of `DnspythonResolver.query`.

use std::fmt;

use nyxmon_core::config::dns::QueryType;

/// RR type codes beyond [`QueryType`].
const TYPE_OPT: u16 = 41;
/// Class IN.
pub const CLASS_IN: u16 = 1;

/// Header flag bits.
pub const FLAG_QR: u16 = 0x8000;
pub const FLAG_TC: u16 = 0x0200;
pub const FLAG_RD: u16 = 0x0100;

/// `dns.rcode` values the resolver distinguishes.
pub const RCODE_NOERROR: u16 = 0;
pub const RCODE_FORMERR: u16 = 1;
pub const RCODE_SERVFAIL: u16 = 2;
pub const RCODE_NXDOMAIN: u16 = 3;
pub const RCODE_NOTIMP: u16 = 4;
pub const RCODE_REFUSED: u16 = 5;
pub const RCODE_YXDOMAIN: u16 = 6;

/// `dns.message.MAX_CHAIN`.
const MAX_CHAIN: usize = 16;

/// An absolute domain name as wire labels (without the root label).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Name {
    pub labels: Vec<Vec<u8>>,
}

impl Name {
    /// dnspython name equality: labels compared with ASCII case folding.
    pub fn eq_ignore_case(&self, other: &Name) -> bool {
        self.labels.len() == other.labels.len()
            && self
                .labels
                .iter()
                .zip(&other.labels)
                .all(|(a, b)| a.eq_ignore_ascii_case(b))
    }

    /// `Name.to_text()` of an absolute name: labels escaped by `_escapify`,
    /// a final dot, `"."` for the root.
    pub fn to_text(&self) -> String {
        if self.labels.is_empty() {
            return ".".to_owned();
        }
        let mut text = String::new();
        for label in &self.labels {
            escapify(label, &mut text);
            text.push('.');
        }
        text
    }

    pub(crate) fn write_wire(&self, out: &mut Vec<u8>) {
        for label in &self.labels {
            out.push(u8::try_from(label.len()).expect("labels are at most 63 bytes"));
            out.extend_from_slice(label);
        }
        out.push(0);
    }

    fn lowercase_wire(&self) -> Vec<u8> {
        let mut out = Vec::new();
        self.write_wire(&mut out);
        out.make_ascii_lowercase();
        out
    }
}

impl fmt::Display for Name {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.to_text())
    }
}

/// `dns.name._escapify` for a wire label.
fn escapify(label: &[u8], out: &mut String) {
    for &byte in label {
        if b"\"().;\\@$".contains(&byte) {
            out.push('\\');
            out.push(char::from(byte));
        } else if byte > 0x20 && byte < 0x7f {
            out.push(char::from(byte));
        } else {
            out.push_str(&format!("\\{byte:03}"));
        }
    }
}

/// Why `dns.name.from_text` (and the root concatenation) rejected a name;
/// `Display` is `str(exc)`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum NameTextError {
    EmptyLabel,
    LabelTooLong,
    NameTooLong,
    BadEscape,
    /// `struct.pack("!B", n)` of an escape `\DDD` above 255 (`struct.error`).
    EscapeOverflow,
    /// A name with non-ASCII characters (dnspython applies IDNA 2008
    /// "practical"); the Rust agent does not port that codec and fails
    /// closed (PLAN.md D-16).
    NonAscii,
}

impl fmt::Display for NameTextError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            NameTextError::EmptyLabel => "A DNS label is empty.",
            NameTextError::LabelTooLong => "A DNS label is > 63 octets long.",
            NameTextError::NameTooLong => "A DNS name is > 255 octets long.",
            NameTextError::BadEscape => "An escaped code in a text format of DNS name is invalid.",
            NameTextError::EscapeOverflow => "'B' format requires 0 <= number <= 255",
            NameTextError::NonAscii => {
                "non-ASCII DNS names are not supported; use the IDNA (xn--) form"
            }
        })
    }
}

/// `_validate_labels`: label length first, then the total length.
fn validate_labels(labels: &[Vec<u8>], absolute: bool) -> Result<(), NameTextError> {
    let mut total = 0;
    for label in labels {
        total += label.len() + 1;
        if label.len() > 63 {
            return Err(NameTextError::LabelTooLong);
        }
    }
    if absolute {
        total += 1;
    }
    if total > 255 {
        return Err(NameTextError::NameTooLong);
    }
    Ok(())
}

/// `dns.name.from_text(text, None)` made absolute as `_get_qnames_to_try`
/// does (`qname.concatenate(dns.name.root)` for a relative name).
///
/// # Errors
/// The dnspython exception, see [`NameTextError`].
pub fn name_from_text(text: &str) -> Result<Name, NameTextError> {
    if !text.is_ascii() {
        return Err(NameTextError::NonAscii);
    }
    let bytes = text.as_bytes();
    let bytes: &[u8] = if bytes == b"@" { b"" } else { bytes };
    let mut labels: Vec<Vec<u8>> = Vec::new();
    let mut absolute = false;
    if bytes == b"." {
        return Ok(Name { labels });
    }
    if !bytes.is_empty() {
        let mut label: Vec<u8> = Vec::new();
        let mut escaping = false;
        let mut edigits = 0;
        let mut total: u32 = 0;
        for &byte in bytes {
            if escaping {
                if edigits == 0 {
                    if byte.is_ascii_digit() {
                        total = u32::from(byte - b'0');
                        edigits += 1;
                    } else {
                        label.push(byte);
                        escaping = false;
                    }
                } else {
                    if !byte.is_ascii_digit() {
                        return Err(NameTextError::BadEscape);
                    }
                    total = total * 10 + u32::from(byte - b'0');
                    edigits += 1;
                    if edigits == 3 {
                        escaping = false;
                        label.push(u8::try_from(total).map_err(|_| NameTextError::EscapeOverflow)?);
                    }
                }
            } else if byte == b'.' {
                if label.is_empty() {
                    return Err(NameTextError::EmptyLabel);
                }
                labels.push(std::mem::take(&mut label));
            } else if byte == b'\\' {
                escaping = true;
                edigits = 0;
                total = 0;
            } else {
                label.push(byte);
            }
        }
        if escaping {
            return Err(NameTextError::BadEscape);
        }
        if label.is_empty() {
            absolute = true;
        } else {
            labels.push(label);
        }
    }
    // `Name(labels)` validates the name as written, the concatenation with
    // the root again (one more octet).
    validate_labels(&labels, absolute)?;
    validate_labels(&labels, true)?;
    Ok(Name { labels })
}

/// One question of a message.
#[derive(Clone, Debug)]
pub struct Question {
    pub name: Name,
    pub rtype: u16,
    pub class: u16,
}

impl Question {
    /// RRset equality of question entries (`n in other.question`).
    fn same(&self, other: &Question) -> bool {
        self.rtype == other.rtype
            && self.class == other.class
            && self.name.eq_ignore_case(&other.name)
    }
}

/// The record data of the types the check renders.
#[derive(Clone, Debug)]
pub enum RData {
    A([u8; 4]),
    Aaaa([u8; 16]),
    Mx {
        preference: u16,
        exchange: Name,
    },
    Txt(Vec<Vec<u8>>),
    /// CNAME, NS, PTR.
    Target(Name),
    Soa {
        mname: Name,
        rname: Name,
        serial: u32,
        refresh: u32,
        retry: u32,
        expire: u32,
        minimum: u32,
    },
    /// Any other type, kept as raw bytes.
    Other(Vec<u8>),
}

impl RData {
    /// The canonical form dnspython compares rdatas by (names lowercased),
    /// used to drop duplicates within an RRset.
    fn canonical(&self) -> Vec<u8> {
        match self {
            RData::A(bytes) => bytes.to_vec(),
            RData::Aaaa(bytes) => bytes.to_vec(),
            RData::Mx {
                preference,
                exchange,
            } => {
                let mut out = preference.to_be_bytes().to_vec();
                out.extend(exchange.lowercase_wire());
                out
            }
            RData::Txt(strings) => {
                let mut out = Vec::new();
                for s in strings {
                    out.push(u8::try_from(s.len()).unwrap_or(u8::MAX));
                    out.extend_from_slice(s);
                }
                out
            }
            RData::Target(name) => name.lowercase_wire(),
            RData::Soa {
                mname,
                rname,
                serial,
                refresh,
                retry,
                expire,
                minimum,
            } => {
                let mut out = mname.lowercase_wire();
                out.extend(rname.lowercase_wire());
                for n in [serial, refresh, retry, expire, minimum] {
                    out.extend(n.to_be_bytes());
                }
                out
            }
            RData::Other(bytes) => bytes.clone(),
        }
    }
}

/// One resource record.
#[derive(Clone, Debug)]
pub struct Record {
    pub name: Name,
    pub rtype: u16,
    pub class: u16,
    pub rdata: RData,
}

/// A parsed message.
#[derive(Clone, Debug)]
pub struct Message {
    pub id: u16,
    pub flags: u16,
    pub questions: Vec<Question>,
    pub answers: Vec<Record>,
    pub authority: Vec<Record>,
    /// `ednsflags` of the OPT record (`None` without EDNS).
    pub edns_flags: Option<u32>,
    /// The OPT record's class (the requestor's payload size).
    pub edns_payload: u16,
}

impl Message {
    /// `Message.rcode()`: the header rcode with the EDNS extended bits.
    pub fn rcode(&self) -> u16 {
        let low = self.flags & 0x000f;
        let high = self
            .edns_flags
            .map_or(0, |flags| ((flags >> 20) & 0x0ff0) as u16);
        low | high
    }

    fn opcode(&self) -> u16 {
        (self.flags >> 11) & 0x0f
    }
}

/// What `from_wire` saw of a message it rejected: the header and the
/// questions parsed before the error (dnspython raises `Truncated` with
/// this partial message when the TC bit is set).
#[derive(Clone, Debug)]
pub struct Partial {
    pub message: Option<Message>,
}

/// The query of `dns.message.make_query(qname, rdtype)` with the resolver's
/// EDNS setting (`edns`: `Some(payload)` for EDNS version 0).
pub fn build_query(id: u16, name: &Name, query_type: QueryType, edns: Option<u16>) -> Vec<u8> {
    let mut out = Vec::with_capacity(64);
    out.extend(id.to_be_bytes());
    out.extend(FLAG_RD.to_be_bytes());
    out.extend(1u16.to_be_bytes());
    out.extend(0u16.to_be_bytes());
    out.extend(0u16.to_be_bytes());
    out.extend(u16::from(edns.is_some()).to_be_bytes());
    name.write_wire(&mut out);
    out.extend(query_type.code().to_be_bytes());
    out.extend(CLASS_IN.to_be_bytes());
    if let Some(payload) = edns {
        out.push(0);
        out.extend(TYPE_OPT.to_be_bytes());
        out.extend(payload.to_be_bytes());
        out.extend(0u32.to_be_bytes());
        out.extend(0u16.to_be_bytes());
    }
    out
}

struct Reader<'a> {
    wire: &'a [u8],
    pos: usize,
    /// Smallest offset a compression pointer may target (`dns.wire.Parser`
    /// only follows pointers backwards).
    end: usize,
}

#[derive(Debug)]
struct FormError;

impl<'a> Reader<'a> {
    fn take(&mut self, n: usize) -> Result<&'a [u8], FormError> {
        if self.end - self.pos < n {
            return Err(FormError);
        }
        let slice = &self.wire[self.pos..self.pos + n];
        self.pos += n;
        Ok(slice)
    }

    fn u8(&mut self) -> Result<u8, FormError> {
        Ok(self.take(1)?[0])
    }

    fn u16(&mut self) -> Result<u16, FormError> {
        let bytes = self.take(2)?;
        Ok(u16::from_be_bytes([bytes[0], bytes[1]]))
    }

    fn u32(&mut self) -> Result<u32, FormError> {
        let bytes = self.take(4)?;
        Ok(u32::from_be_bytes([bytes[0], bytes[1], bytes[2], bytes[3]]))
    }

    /// `dns.name.from_wire_parser`: labels with backward-only compression
    /// pointers, validated like `Name()`.
    fn name(&mut self) -> Result<Name, FormError> {
        let mut labels = Vec::new();
        let mut cursor = self.pos;
        let mut limit = self.pos;
        let mut jumped = false;
        loop {
            if !jumped && cursor >= self.end {
                return Err(FormError);
            }
            let len = *self.wire.get(cursor).ok_or(FormError)?;
            if len >= 0xc0 {
                let low = *self.wire.get(cursor + 1).ok_or(FormError)?;
                if !jumped && cursor + 2 > self.end {
                    return Err(FormError);
                }
                let target = (usize::from(len & 0x3f) << 8) | usize::from(low);
                if target >= limit {
                    return Err(FormError);
                }
                if !jumped {
                    self.pos = cursor + 2;
                    jumped = true;
                }
                limit = target;
                cursor = target;
            } else if len >= 0x40 {
                return Err(FormError);
            } else {
                let start = cursor + 1;
                let stop = start + usize::from(len);
                let bound = if jumped { self.wire.len() } else { self.end };
                if stop > bound {
                    return Err(FormError);
                }
                if len == 0 {
                    if !jumped {
                        self.pos = start;
                    }
                    break;
                }
                labels.push(self.wire[start..stop].to_vec());
                cursor = stop;
            }
        }
        validate_labels(&labels, true).map_err(|_| FormError)?;
        Ok(Name { labels })
    }

    fn question(&mut self) -> Result<Question, FormError> {
        let name = self.name()?;
        let rtype = self.u16()?;
        let class = self.u16()?;
        Ok(Question { name, rtype, class })
    }
}

/// The rdata of one record, consuming exactly `len` bytes.
fn rdata(reader: &mut Reader<'_>, rtype: u16, len: usize) -> Result<RData, FormError> {
    let end = reader
        .pos
        .checked_add(len)
        .filter(|e| *e <= reader.end)
        .ok_or(FormError)?;
    let outer_end = reader.end;
    reader.end = end;
    let parsed = (|| -> Result<RData, FormError> {
        let value = match rtype {
            1 => RData::A(reader.take(4)?.try_into().map_err(|_| FormError)?),
            28 => RData::Aaaa(reader.take(16)?.try_into().map_err(|_| FormError)?),
            15 => RData::Mx {
                preference: reader.u16()?,
                exchange: reader.name()?,
            },
            2 | 5 | 12 => RData::Target(reader.name()?),
            6 => RData::Soa {
                mname: reader.name()?,
                rname: reader.name()?,
                serial: reader.u32()?,
                refresh: reader.u32()?,
                retry: reader.u32()?,
                expire: reader.u32()?,
                minimum: reader.u32()?,
            },
            16 => {
                let mut strings = Vec::new();
                while reader.pos < reader.end {
                    let n = usize::from(reader.u8()?);
                    strings.push(reader.take(n)?.to_vec());
                }
                if strings.is_empty() {
                    return Err(FormError);
                }
                RData::Txt(strings)
            }
            _ => RData::Other(reader.take(reader.end - reader.pos)?.to_vec()),
        };
        if reader.pos != reader.end {
            return Err(FormError);
        }
        Ok(value)
    })();
    reader.end = outer_end;
    parsed
}

/// `dns.message.from_wire(wire)` for the parts the check uses.
///
/// # Errors
/// Any message dnspython would reject (short header, bad names, bad rdata
/// of the rendered types, misplaced or repeated OPT, trailing bytes), with
/// the part parsed so far.
pub fn parse_message(wire: &[u8]) -> Result<Message, Partial> {
    let mut reader = Reader {
        wire,
        pos: 0,
        end: wire.len(),
    };
    let header = (|| -> Result<[u16; 6], FormError> {
        Ok([
            reader.u16()?,
            reader.u16()?,
            reader.u16()?,
            reader.u16()?,
            reader.u16()?,
            reader.u16()?,
        ])
    })();
    let Ok([id, flags, qdcount, ancount, nscount, arcount]) = header else {
        return Err(Partial { message: None });
    };
    let mut message = Message {
        id,
        flags,
        questions: Vec::new(),
        answers: Vec::new(),
        authority: Vec::new(),
        edns_flags: None,
        edns_payload: 0,
    };
    for _ in 0..qdcount {
        match reader.question() {
            Ok(question) => message.questions.push(question),
            Err(FormError) => {
                return Err(Partial {
                    message: Some(message),
                });
            }
        }
    }
    let mut body = || -> Result<(), FormError> {
        for (section, count) in [(0, ancount), (1, nscount), (2, arcount)] {
            for _ in 0..count {
                let name = reader.name()?;
                let rtype = reader.u16()?;
                let class = reader.u16()?;
                let ttl = reader.u32()?;
                let len = usize::from(reader.u16()?);
                if rtype == TYPE_OPT {
                    if section != 2 || message.edns_flags.is_some() || !name.labels.is_empty() {
                        return Err(FormError);
                    }
                    reader.take(len)?;
                    message.edns_flags = Some(ttl);
                    message.edns_payload = class;
                    continue;
                }
                let rdata = rdata(&mut reader, rtype, len)?;
                let record = Record {
                    name,
                    rtype,
                    class,
                    rdata,
                };
                match section {
                    0 => message.answers.push(record),
                    1 => message.authority.push(record),
                    _ => {}
                }
            }
        }
        if reader.pos != wire.len() {
            return Err(FormError);
        }
        Ok(())
    };
    match body() {
        Ok(()) => Ok(message),
        Err(FormError) => Err(Partial {
            message: Some(message),
        }),
    }
}

/// `query.is_response(response)`.
pub fn is_response(query_id: u16, question: &Question, response: &Message) -> bool {
    if response.flags & FLAG_QR == 0 || response.id != query_id || response.opcode() != 0 {
        return false;
    }
    if matches!(
        response.rcode(),
        RCODE_FORMERR | RCODE_SERVFAIL | RCODE_NOTIMP | RCODE_REFUSED
    ) && response.questions.is_empty()
    {
        return true;
    }
    response.questions.iter().any(|q| q.same(question))
        && response.questions.iter().all(|q| q.same(question))
}

/// `find_rrset(section, name, IN, rtype)`: the rdatas of matching records
/// in order, duplicates dropped; `None` when there is none.
fn find_rrset<'a>(section: &'a [Record], name: &Name, rtype: u16) -> Option<Vec<&'a RData>> {
    let mut found: Vec<&RData> = Vec::new();
    let mut seen: Vec<Vec<u8>> = Vec::new();
    let mut any = false;
    for record in section {
        if record.rtype == rtype && record.class == CLASS_IN && record.name.eq_ignore_case(name) {
            any = true;
            let canonical = record.rdata.canonical();
            if !seen.contains(&canonical) {
                seen.push(canonical);
                found.push(&record.rdata);
            }
        }
    }
    any.then_some(found)
}

/// Why `Message.resolve_chaining()` rejected a response.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ChainError {
    /// `FormError`: not exactly one question.
    QuestionCount,
    ChainTooLong,
    AnswerForNxdomain,
}

impl fmt::Display for ChainError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            ChainError::QuestionCount => "The DNS packet passed to from_wire() is invalid.",
            ChainError::ChainTooLong => "The CNAME chain is too long.",
            ChainError::AnswerForNxdomain => "The rcode is NXDOMAIN but an answer was found.",
        })
    }
}

/// `Message.resolve_chaining()`: the answer RRset after following CNAMEs
/// from the response's question name (`None` when there is none).
///
/// # Errors
/// See [`ChainError`].
pub fn resolve_chaining(response: &Message) -> Result<Option<Vec<&RData>>, ChainError> {
    let [question] = response.questions.as_slice() else {
        return Err(ChainError::QuestionCount);
    };
    let mut qname = question.name.clone();
    let mut answer = None;
    let mut count = 0;
    while count < MAX_CHAIN {
        if question.class == CLASS_IN
            && let Some(rrset) = find_rrset(&response.answers, &qname, question.rtype)
        {
            answer = Some(rrset);
            break;
        }
        if question.rtype == QueryType::Cname.code() || question.class != CLASS_IN {
            break;
        }
        let Some(cnames) = find_rrset(&response.answers, &qname, QueryType::Cname.code()) else {
            break;
        };
        match cnames.first() {
            Some(RData::Target(target)) => qname = target.clone(),
            _ => break,
        }
        count += 1;
    }
    if count >= MAX_CHAIN {
        return Err(ChainError::ChainTooLong);
    }
    if response.rcode() == RCODE_NXDOMAIN && answer.is_some() {
        return Err(ChainError::AnswerForNxdomain);
    }
    Ok(answer)
}

/// `dns.ipv6.inet_ntoa`.
pub fn ipv6_text(address: &[u8; 16]) -> String {
    let chunks: Vec<String> = address
        .chunks(2)
        .map(|pair| format!("{:x}", u16::from_be_bytes([pair[0], pair[1]])))
        .collect();
    let (mut best_start, mut best_len) = (0usize, 0usize);
    let mut start = 0usize;
    let mut last_was_zero = false;
    for (i, chunk) in chunks.iter().enumerate() {
        if chunk != "0" {
            if last_was_zero {
                if i - start > best_len {
                    best_start = start;
                    best_len = i - start;
                }
                last_was_zero = false;
            }
        } else if !last_was_zero {
            start = i;
            last_was_zero = true;
        }
    }
    if last_was_zero && 8 - start > best_len {
        best_start = start;
        best_len = 8 - start;
    }
    if best_len > 1 {
        if best_start == 0 && (best_len == 6 || best_len == 5 && chunks[5] == "ffff") {
            let prefix = if best_len == 6 { "::" } else { "::ffff:" };
            return format!(
                "{prefix}{}.{}.{}.{}",
                address[12], address[13], address[14], address[15]
            );
        }
        return format!(
            "{}::{}",
            chunks[..best_start].join(":"),
            chunks[best_start + best_len..].join(":")
        );
    }
    chunks.join(":")
}

/// `bytes.decode()` (strict UTF-8) as CPython reports a failure:
/// `'utf-8' codec can't decode byte 0x.. in position N: <reason>`.
///
/// # Errors
/// The `UnicodeDecodeError` text.
pub fn py_utf8_decode(bytes: &[u8]) -> Result<String, String> {
    match std::str::from_utf8(bytes) {
        Ok(text) => Ok(text.to_owned()),
        Err(err) => {
            let start = err.valid_up_to();
            let byte = bytes[start];
            let (end, reason) = match err.error_len() {
                None => (bytes.len(), "unexpected end of data"),
                Some(len) if !(0xc2..=0xf4).contains(&byte) => (start + len, "invalid start byte"),
                Some(len) => (start + len, "invalid continuation byte"),
            };
            Err(if end - start == 1 {
                format!(
                    "'utf-8' codec can't decode byte 0x{byte:02x} in position {start}: {reason}"
                )
            } else {
                format!(
                    "'utf-8' codec can't decode bytes in position {start}-{}: {reason}",
                    end - 1
                )
            })
        }
    }
}

/// The record texts of `DnspythonResolver.query` for an answer RRset.
///
/// # Errors
/// The `UnicodeDecodeError` text of a TXT string that is not UTF-8 (a
/// `ValueError`: the executor reports `configuration_error`).
pub fn record_texts(rdatas: &[&RData]) -> Result<Vec<String>, String> {
    let mut texts = Vec::with_capacity(rdatas.len());
    for rdata in rdatas {
        texts.push(match rdata {
            RData::A(octets) => {
                format!("{}.{}.{}.{}", octets[0], octets[1], octets[2], octets[3])
            }
            RData::Aaaa(bytes) => ipv6_text(bytes),
            RData::Mx { exchange, .. } => exchange.to_text(),
            RData::Txt(strings) => strings
                .iter()
                .map(|s| py_utf8_decode(s))
                .collect::<Result<Vec<_>, _>>()?
                .join(" "),
            RData::Target(name) => name.to_text(),
            RData::Soa {
                mname,
                rname,
                serial,
                refresh,
                retry,
                expire,
                minimum,
            } => format!("{mname} {rname} {serial} {refresh} {retry} {expire} {minimum}"),
            RData::Other(_) => String::new(),
        });
    }
    Ok(texts)
}

#[cfg(test)]
mod tests;
