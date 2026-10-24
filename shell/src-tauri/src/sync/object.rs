//! One shared object, as it lies on the drive (spec: Shared storage and sync, "What is
//! shared: immutable objects" and "Encryption"):
//!
//! ```text
//! devices/<laptop-id>/<sequence>-<object-key>.fio
//!
//! header   "FIO1" | laptop id (16) | sequence (u64 LE) | fleet key id (16)
//!          | content key sealed with that fleet key (72) | stream header (24)
//! body     the plaintext in 64 KiB chunks, XChaCha20-Poly1305 secretstream;
//!          only the last chunk is tagged FINAL
//! trailer  Ed25519 signature by the writing laptop over
//!          "fi.fio/1" | header | BLAKE2b-256(body)
//! ```
//!
//! The object key is HMAC-SHA-256 of the plaintext under a key derived from the fleet:
//! the same content has the same name on every laptop, and Google cannot test whether
//! a known file is there. Reading checks, in order: the name against the header, the
//! signature (before decrypting anything), each chunk, the stream's end, and the
//! plaintext against its name. A refusal says which check failed, never a value.

use std::fs::File;
use std::io::{self, BufReader, Read, Seek, SeekFrom, Write};
use std::path::Path;

use crate::sync::sodium::{self, Hash, Hmac, Pull, Push, STREAM_ABYTES, STREAM_HEADER};

pub const MAGIC: &[u8; 4] = b"FIO1";
pub const CHUNK: usize = 64 * 1024;
const SIGNED: &[u8] = b"fi.fio/1";
const WRAPPED: usize = sodium::SECRETBOX_NONCE + sodium::SECRETBOX_MAC + 32;
pub const HEADER: usize = 4 + 16 + 8 + 16 + WRAPPED + STREAM_HEADER;
pub const SIGNATURE: usize = 64;

/// Where an object lies and what it claims to be.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Name {
    pub laptop: [u8; 16],
    pub sequence: u64,
    pub object_key: [u8; 32],
}

impl Name {
    pub fn path(&self) -> String {
        format!(
            "devices/{}/{:012}-{}.fio",
            hex::encode(self.laptop),
            self.sequence,
            hex::encode(self.object_key)
        )
    }

    /// The name from a drive path; None for anything not shaped like one.
    pub fn parse(path: &str) -> Option<Name> {
        let mut parts = path.split('/');
        let (devices, laptop, file) = (parts.next()?, parts.next()?, parts.next()?);
        if devices != "devices" || parts.next().is_some() {
            return None;
        }
        let (sequence, key) = file.strip_suffix(".fio")?.split_once('-')?;
        if sequence.len() != 12 || !sequence.bytes().all(|b| b.is_ascii_digit()) {
            return None;
        }
        let mut name = Name {
            laptop: [0; 16],
            sequence: sequence.parse().ok()?,
            object_key: [0; 32],
        };
        if laptop.len() != 32
            || key.len() != 64
            || laptop != laptop.to_lowercase()
            || key != key.to_lowercase()
        {
            return None;
        }
        hex::decode_to_slice(laptop, &mut name.laptop).ok()?;
        hex::decode_to_slice(key, &mut name.object_key).ok()?;
        Some(name)
    }
}

/// The fleet key an object is written with.
pub struct FleetKey<'a> {
    pub id: [u8; 16],
    pub key: &'a [u8; 32],
}

/// Why an object was refused: which check, never a value from it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Refusal(pub &'static str);

impl std::fmt::Display for Refusal {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.0)
    }
}

fn read_full(input: &mut impl Read, buf: &mut [u8]) -> io::Result<usize> {
    let mut n = 0;
    while n < buf.len() {
        match input.read(&mut buf[n..])? {
            0 => break,
            k => n += k,
        }
    }
    Ok(n)
}

/// Encrypts and signs `plaintext` into `out`; returns the object's name. `sign` is the
/// writing laptop's key; `object_keys` the fleet's key for object names.
pub fn write(
    out: &mut impl Write,
    plaintext: &mut impl Read,
    laptop: [u8; 16],
    sequence: u64,
    fleet: &FleetKey,
    object_keys: &[u8; 32],
    sign: &dyn Fn(&[u8]) -> [u8; 64],
) -> io::Result<Name> {
    let content = sodium::random::<32>();
    let (mut push, stream_header) = Push::new(&content);
    let mut header = Vec::with_capacity(HEADER);
    header.extend_from_slice(MAGIC);
    header.extend_from_slice(&laptop);
    header.extend_from_slice(&sequence.to_le_bytes());
    header.extend_from_slice(&fleet.id);
    header.extend_from_slice(&sodium::secretbox_seal(fleet.key, content.as_ref()));
    header.extend_from_slice(&stream_header);
    debug_assert_eq!(header.len(), HEADER);
    out.write_all(&header)?;

    let mut body = Hash::new(None);
    let mut mac = Hmac::new(object_keys);
    let mut current = vec![0u8; CHUNK];
    let mut next = vec![0u8; CHUNK];
    let mut have = read_full(plaintext, &mut current)?;
    loop {
        // Read ahead one chunk: only the stream's last chunk is tagged FINAL.
        let ahead = if have == CHUNK {
            read_full(plaintext, &mut next)?
        } else {
            0
        };
        let last = ahead == 0;
        mac.update(&current[..have]);
        let sealed = push.push(&current[..have], last);
        body.update(&sealed);
        out.write_all(&sealed)?;
        if last {
            break;
        }
        std::mem::swap(&mut current, &mut next);
        have = ahead;
    }
    let mut signed = Vec::with_capacity(SIGNED.len() + HEADER + 32);
    signed.extend_from_slice(SIGNED);
    signed.extend_from_slice(&header);
    signed.extend_from_slice(&body.finish());
    out.write_all(&sign(&signed))?;
    out.flush()?;
    Ok(Name {
        laptop,
        sequence,
        object_key: mac.finish(),
    })
}

/// What a reader needs from the keyring and the device registry.
pub trait Keys {
    /// Whether this laptop may have written this sequence, and its signature holds.
    fn check_signature(
        &self,
        laptop: &[u8; 16],
        sequence: u64,
        signed: &[u8],
        signature: &[u8; 64],
    ) -> Result<(), Refusal>;
    fn fleet_key(&self, id: &[u8; 16]) -> Option<&[u8; 32]>;
    fn object_keys(&self) -> &[u8; 32];
}

/// Checks and decrypts the object at `path` into `out`. On a refusal, whatever was
/// written to `out` must be thrown away: the caller writes to staging.
pub fn read(
    path: &Path,
    name: &Name,
    keys: &dyn Keys,
    out: &mut impl Write,
) -> Result<(), Refusal> {
    let io = |_: io::Error| Refusal("the object could not be read");
    let mut file = BufReader::new(File::open(path).map_err(io)?);
    let size = file.get_ref().metadata().map_err(io)?.len();
    if size < (HEADER + STREAM_ABYTES + SIGNATURE) as u64 {
        return Err(Refusal("too short to be an object"));
    }
    let mut header = [0u8; HEADER];
    file.read_exact(&mut header).map_err(io)?;
    if &header[..4] != MAGIC {
        return Err(Refusal("not an object this app reads"));
    }
    let laptop: [u8; 16] = header[4..20].try_into().expect("16 bytes");
    let sequence = u64::from_le_bytes(header[20..28].try_into().expect("8 bytes"));
    if laptop != name.laptop || sequence != name.sequence {
        return Err(Refusal("its name and its header disagree"));
    }
    let key_id: [u8; 16] = header[28..44].try_into().expect("16 bytes");
    let wrapped = &header[44..44 + WRAPPED];
    let stream_header: [u8; STREAM_HEADER] = header[44 + WRAPPED..].try_into().expect("header");

    // 1. The signature, over the header and the whole body, before decrypting anything.
    let body_len = size - (HEADER + SIGNATURE) as u64;
    let mut hash = Hash::new(None);
    let mut buf = vec![0u8; CHUNK + STREAM_ABYTES];
    let mut left = body_len;
    while left > 0 {
        let n = left.min(buf.len() as u64) as usize;
        file.read_exact(&mut buf[..n]).map_err(io)?;
        hash.update(&buf[..n]);
        left -= n as u64;
    }
    let mut signature = [0u8; SIGNATURE];
    file.read_exact(&mut signature).map_err(io)?;
    let mut signed = Vec::with_capacity(SIGNED.len() + HEADER + 32);
    signed.extend_from_slice(SIGNED);
    signed.extend_from_slice(&header);
    signed.extend_from_slice(&hash.finish());
    keys.check_signature(&laptop, sequence, &signed, &signature)?;

    // 2. The content key, then every chunk in order; only the last may be FINAL.
    let fleet = keys.fleet_key(&key_id).ok_or(Refusal(
        "written with a fleet key this laptop does not hold",
    ))?;
    let content = sodium::secretbox_open(fleet, wrapped)
        .map_err(|_| Refusal("its content key does not open with the fleet key"))?;
    let content: &[u8; 32] = content
        .as_slice()
        .try_into()
        .map_err(|_| Refusal("its content key is the wrong size"))?;
    let mut pull =
        Pull::new(content, &stream_header).map_err(|_| Refusal("its stream header is damaged"))?;
    file.seek(SeekFrom::Start(HEADER as u64)).map_err(io)?;
    let mut mac = Hmac::new(keys.object_keys());
    let mut left = body_len;
    let mut ended = false;
    while left > 0 {
        if ended {
            return Err(Refusal("data after the stream's end"));
        }
        let n = left.min((CHUNK + STREAM_ABYTES) as u64) as usize;
        file.read_exact(&mut buf[..n]).map_err(io)?;
        let (plain, last) = pull
            .pull(&buf[..n])
            .map_err(|_| Refusal("a chunk failed its check"))?;
        mac.update(&plain);
        out.write_all(&plain)
            .map_err(|_| Refusal("the plaintext could not be staged"))?;
        ended = last;
        left -= n as u64;
    }
    if !ended {
        return Err(Refusal("the stream ends early"));
    }
    // 3. The plaintext is the one its name says.
    if !sodium::equal(&mac.finish(), &name.object_key) {
        return Err(Refusal("its content does not match its name"));
    }
    out.flush()
        .map_err(|_| Refusal("the plaintext could not be staged"))?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use ed25519_dalek::{Signature, Signer, SigningKey, Verifier};
    use std::collections::HashMap;

    struct Test {
        signer: SigningKey,
        laptop: [u8; 16],
        revoked_after: Option<u64>,
        fleet: HashMap<[u8; 16], [u8; 32]>,
        names: [u8; 32],
    }

    impl Keys for Test {
        fn check_signature(
            &self,
            laptop: &[u8; 16],
            sequence: u64,
            signed: &[u8],
            sig: &[u8; 64],
        ) -> Result<(), Refusal> {
            if laptop != &self.laptop {
                return Err(Refusal("written by a laptop that is not enrolled"));
            }
            if self.revoked_after.is_some_and(|last| sequence > last) {
                return Err(Refusal("written after its laptop was revoked"));
            }
            self.signer
                .verifying_key()
                .verify(signed, &Signature::from_bytes(sig))
                .map_err(|_| Refusal("its signature does not hold"))
        }
        fn fleet_key(&self, id: &[u8; 16]) -> Option<&[u8; 32]> {
            self.fleet.get(id)
        }
        fn object_keys(&self) -> &[u8; 32] {
            &self.names
        }
    }

    fn setup() -> Test {
        let mut fleet = HashMap::new();
        fleet.insert([1u8; 16], *sodium::random::<32>());
        Test {
            signer: SigningKey::from_bytes(&sodium::random::<32>()),
            laptop: [9u8; 16],
            revoked_after: None,
            fleet,
            names: *sodium::random::<32>(),
        }
    }

    fn written(t: &Test, plaintext: &[u8], sequence: u64) -> (Vec<u8>, Name) {
        let mut out = Vec::new();
        let key = t.fleet[&[1u8; 16]];
        let name = write(
            &mut out,
            &mut &plaintext[..],
            t.laptop,
            sequence,
            &FleetKey {
                id: [1u8; 16],
                key: &key,
            },
            &t.names,
            &|m| t.signer.sign(m).to_bytes(),
        )
        .unwrap();
        (out, name)
    }

    fn opened(t: &Test, bytes: &[u8], name: &Name) -> Result<Vec<u8>, Refusal> {
        let dir = std::env::temp_dir().join(format!(
            "fio-{}",
            hex::encode(sodium::random::<8>().as_ref())
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("o.fio");
        std::fs::write(&path, bytes).unwrap();
        let mut out = Vec::new();
        let got = read(&path, name, t, &mut out).map(|()| out);
        std::fs::remove_dir_all(&dir).unwrap();
        got
    }

    fn sizes() -> Vec<usize> {
        vec![0, 1, CHUNK - 1, CHUNK, CHUNK + 1, 3 * CHUNK, 3 * CHUNK + 7]
    }

    #[test]
    fn objects_round_trip_at_every_chunk_boundary() {
        let t = setup();
        for n in sizes() {
            let plain: Vec<u8> = (0..n).map(|i| (i * 31 % 251) as u8).collect();
            let (bytes, name) = written(&t, &plain, 7);
            assert_eq!(opened(&t, &bytes, &name).unwrap(), plain, "{n} bytes");
            assert_eq!(Name::parse(&name.path()), Some(name));
        }
    }

    #[test]
    fn the_same_content_has_the_same_name_and_different_bytes() {
        let t = setup();
        let (a, na) = written(&t, b"same log", 1);
        let (b, nb) = written(&t, b"same log", 1);
        assert_eq!(na.object_key, nb.object_key);
        assert_ne!(a, b, "a fresh content key every time");
    }

    #[test]
    fn every_single_byte_change_is_refused() {
        let t = setup();
        let plain: Vec<u8> = (0..CHUNK + 300).map(|i| i as u8).collect();
        let (bytes, name) = written(&t, &plain, 3);
        for i in 0..bytes.len() {
            // every byte of header and trailer, and a sample of the body
            if i >= HEADER && i < bytes.len() - SIGNATURE && i % 97 != 0 {
                continue;
            }
            let mut bad = bytes.clone();
            bad[i] ^= 0x40;
            assert!(
                opened(&t, &bad, &name).is_err(),
                "byte {i} changed and still opened"
            );
        }
    }

    #[test]
    fn truncated_reordered_and_misnamed_objects_are_refused() {
        let t = setup();
        let plain: Vec<u8> = (0..3 * CHUNK + 10).map(|i| (i % 7) as u8).collect();
        let (bytes, name) = written(&t, &plain, 4);
        // cut at a chunk boundary, with a signature that holds over what is left
        let cut = HEADER + 2 * (CHUNK + STREAM_ABYTES);
        let mut short = bytes[..cut].to_vec();
        let mut signed = SIGNED.to_vec();
        signed.extend_from_slice(&bytes[..HEADER]);
        signed.extend_from_slice(&Hash::of(&[&bytes[HEADER..cut]]));
        short.extend_from_slice(&t.signer.sign(&signed).to_bytes());
        assert_eq!(
            opened(&t, &short, &name),
            Err(Refusal("the stream ends early"))
        );
        // two chunks swapped, re-signed
        let a = HEADER..HEADER + CHUNK + STREAM_ABYTES;
        let b = a.end..a.end + CHUNK + STREAM_ABYTES;
        let mut swapped = bytes[..HEADER].to_vec();
        swapped.extend_from_slice(&bytes[b.clone()]);
        swapped.extend_from_slice(&bytes[a.clone()]);
        swapped.extend_from_slice(&bytes[b.end..bytes.len() - SIGNATURE]);
        let mut signed = SIGNED.to_vec();
        signed.extend_from_slice(&bytes[..HEADER]);
        signed.extend_from_slice(&Hash::of(&[&swapped[HEADER..]]));
        swapped.extend_from_slice(&t.signer.sign(&signed).to_bytes());
        assert_eq!(
            opened(&t, &swapped, &name),
            Err(Refusal("a chunk failed its check"))
        );
        // a name from another object, or another place
        let (_, other) = written(&t, b"another", 4);
        assert_eq!(
            opened(&t, &bytes, &other),
            Err(Refusal("its content does not match its name"))
        );
        let moved = Name {
            sequence: 5,
            ..name
        };
        assert_eq!(
            opened(&t, &bytes, &moved),
            Err(Refusal("its name and its header disagree"))
        );
    }

    #[test]
    fn wrong_signers_and_keys_are_refused() {
        let t = setup();
        let (bytes, name) = written(&t, b"records", 8);
        let stranger = Test {
            signer: SigningKey::from_bytes(&sodium::random::<32>()),
            ..setup()
        };
        let stranger = Test {
            fleet: t.fleet.clone(),
            names: t.names,
            ..stranger
        };
        assert_eq!(
            opened(&stranger, &bytes, &name),
            Err(Refusal("its signature does not hold"))
        );
        let revoked = Test {
            revoked_after: Some(7),
            signer: t.signer.clone(),
            fleet: t.fleet.clone(),
            ..setup()
        };
        let revoked = Test {
            names: t.names,
            ..revoked
        };
        assert_eq!(
            opened(&revoked, &bytes, &name),
            Err(Refusal("written after its laptop was revoked"))
        );
        let keyless = Test {
            fleet: HashMap::new(),
            signer: t.signer.clone(),
            names: t.names,
            ..setup()
        };
        assert_eq!(
            opened(&keyless, &bytes, &name),
            Err(Refusal(
                "written with a fleet key this laptop does not hold"
            ))
        );
        let mut rotated = HashMap::new();
        rotated.insert([1u8; 16], *sodium::random::<32>());
        let wrong = Test {
            fleet: rotated,
            signer: t.signer.clone(),
            names: t.names,
            ..setup()
        };
        assert_eq!(
            opened(&wrong, &bytes, &name),
            Err(Refusal("its content key does not open with the fleet key"))
        );
    }

    #[test]
    fn names_parse_only_in_their_own_shape() {
        let n = Name {
            laptop: [0xab; 16],
            sequence: 42,
            object_key: [0xcd; 32],
        };
        assert_eq!(Name::parse(&n.path()), Some(n));
        for bad in [
            "devices/x/000000000042-cd.fio",
            &n.path().replace("devices", "device"),
            &n.path().replace(".fio", ".bin"),
            &n.path().replace("000000000042", "42"),
            &n.path().to_uppercase(),
            &format!("{}/extra", n.path()),
        ] {
            assert_eq!(Name::parse(bad), None, "{bad}");
        }
    }
}
