//! libsodium, wrapped (spec: Shared storage and sync, "Encryption"). Every primitive
//! sync uses goes through here, and nothing here is new cryptography: each function is
//! one libsodium call with its buffer sizes checked.
//!
//! Secrets are returned in memory that is wiped when dropped, and the stream states,
//! which hold keys, are wiped too.

use std::sync::OnceLock;

use libsodium_sys as ffi;
use zeroize::Zeroizing;

pub type Key = Zeroizing<[u8; 32]>;

pub const STREAM_HEADER: usize = ffi::crypto_secretstream_xchacha20poly1305_HEADERBYTES as usize;
pub const STREAM_ABYTES: usize = ffi::crypto_secretstream_xchacha20poly1305_ABYTES as usize;
pub const SECRETBOX_NONCE: usize = ffi::crypto_secretbox_NONCEBYTES as usize;
pub const SECRETBOX_MAC: usize = ffi::crypto_secretbox_MACBYTES as usize;
pub const SEAL_BYTES: usize = ffi::crypto_box_SEALBYTES as usize;
const TAG_MESSAGE: u8 = ffi::crypto_secretstream_xchacha20poly1305_TAG_MESSAGE as u8;
const TAG_FINAL: u8 = ffi::crypto_secretstream_xchacha20poly1305_TAG_FINAL as u8;

/// libsodium must be initialised once before any other call.
pub fn init() -> Result<(), String> {
    static READY: OnceLock<bool> = OnceLock::new();
    // SAFETY: sodium_init is thread-safe and may be called more than once.
    if *READY.get_or_init(|| unsafe { ffi::sodium_init() } >= 0) {
        Ok(())
    } else {
        Err("libsodium could not start".into())
    }
}

fn ready() {
    init().expect("libsodium could not start");
}

pub fn random<const N: usize>() -> Zeroizing<[u8; N]> {
    ready();
    let mut out = Zeroizing::new([0u8; N]);
    // SAFETY: writes exactly N bytes into a buffer of N bytes.
    unsafe { ffi::randombytes_buf(out.as_mut_ptr().cast(), N) };
    out
}

/// A subkey of a master key for one purpose (`context`, eight bytes) and number.
pub fn kdf(master: &[u8; 32], id: u64, context: &[u8; 8]) -> Key {
    ready();
    let mut out = Zeroizing::new([0u8; 32]);
    // SAFETY: a 32-byte subkey from a 32-byte key and an 8-byte context, as the API asks.
    let rc = unsafe {
        ffi::crypto_kdf_derive_from_key(
            out.as_mut_ptr(),
            32,
            id,
            context.as_ptr().cast(),
            master.as_ptr(),
        )
    };
    assert_eq!(rc, 0, "crypto_kdf_derive_from_key refused a valid length");
    out
}

/// XSalsa20-Poly1305 with a random nonce: nonce || ciphertext.
pub fn secretbox_seal(key: &[u8; 32], message: &[u8]) -> Vec<u8> {
    ready();
    let nonce = random::<SECRETBOX_NONCE>();
    let mut out = vec![0u8; SECRETBOX_NONCE + SECRETBOX_MAC + message.len()];
    out[..SECRETBOX_NONCE].copy_from_slice(nonce.as_ref());
    // SAFETY: the output holds the MAC plus the message; the nonce and key are their sizes.
    let rc = unsafe {
        ffi::crypto_secretbox_easy(
            out[SECRETBOX_NONCE..].as_mut_ptr(),
            message.as_ptr(),
            message.len() as u64,
            nonce.as_ptr(),
            key.as_ptr(),
        )
    };
    assert_eq!(rc, 0, "crypto_secretbox_easy failed");
    out
}

pub fn secretbox_open(key: &[u8; 32], sealed: &[u8]) -> Result<Zeroizing<Vec<u8>>, String> {
    ready();
    if sealed.len() < SECRETBOX_NONCE + SECRETBOX_MAC {
        return Err("too short to be sealed".into());
    }
    let (nonce, cipher) = sealed.split_at(SECRETBOX_NONCE);
    let mut out = Zeroizing::new(vec![0u8; cipher.len() - SECRETBOX_MAC]);
    // SAFETY: the output is the ciphertext's length less the MAC.
    let rc = unsafe {
        ffi::crypto_secretbox_open_easy(
            out.as_mut_ptr(),
            cipher.as_ptr(),
            cipher.len() as u64,
            nonce.as_ptr(),
            key.as_ptr(),
        )
    };
    if rc == 0 {
        Ok(out)
    } else {
        Err("does not open with this key".into())
    }
}

/// A sealed box: anyone with the public key can seal, only its owner can open.
pub fn seal(public: &[u8; 32], message: &[u8]) -> Vec<u8> {
    ready();
    let mut out = vec![0u8; SEAL_BYTES + message.len()];
    // SAFETY: the output holds the message plus the seal's overhead.
    let rc = unsafe {
        ffi::crypto_box_seal(
            out.as_mut_ptr(),
            message.as_ptr(),
            message.len() as u64,
            public.as_ptr(),
        )
    };
    assert_eq!(rc, 0, "crypto_box_seal failed");
    out
}

pub fn seal_open(
    public: &[u8; 32],
    secret: &[u8; 32],
    sealed: &[u8],
) -> Result<Zeroizing<Vec<u8>>, String> {
    ready();
    if sealed.len() < SEAL_BYTES {
        return Err("too short to be a sealed box".into());
    }
    let mut out = Zeroizing::new(vec![0u8; sealed.len() - SEAL_BYTES]);
    // SAFETY: the output is the sealed length less the overhead; both keys are 32 bytes.
    let rc = unsafe {
        ffi::crypto_box_seal_open(
            out.as_mut_ptr(),
            sealed.as_ptr(),
            sealed.len() as u64,
            public.as_ptr(),
            secret.as_ptr(),
        )
    };
    if rc == 0 {
        Ok(out)
    } else {
        Err("not sealed for this laptop".into())
    }
}

pub fn sha256(data: &[u8]) -> [u8; 32] {
    ready();
    let mut out = [0u8; 32];
    // SAFETY: a 32-byte digest of the given bytes.
    unsafe { ffi::crypto_hash_sha256(out.as_mut_ptr(), data.as_ptr(), data.len() as u64) };
    out
}

/// SHA-256 over data fed in parts.
pub struct Sha256(Box<ffi::crypto_hash_sha256_state>);

impl Sha256 {
    pub fn new() -> Self {
        ready();
        // SAFETY: the state is plain bytes that init fills.
        let mut state: Box<ffi::crypto_hash_sha256_state> = Box::new(unsafe { std::mem::zeroed() });
        // SAFETY: the state is a valid crypto_hash_sha256_state.
        unsafe { ffi::crypto_hash_sha256_init(&mut *state) };
        Sha256(state)
    }

    pub fn update(&mut self, data: &[u8]) {
        // SAFETY: the state was initialised by new.
        unsafe { ffi::crypto_hash_sha256_update(&mut *self.0, data.as_ptr(), data.len() as u64) };
    }

    pub fn finish(mut self) -> [u8; 32] {
        let mut out = [0u8; 32];
        // SAFETY: a 32-byte digest.
        unsafe { ffi::crypto_hash_sha256_final(&mut *self.0, out.as_mut_ptr()) };
        out
    }

    /// The SHA-256 of a file, read in parts.
    pub fn of_file(path: &std::path::Path) -> std::io::Result<[u8; 32]> {
        use std::io::Read;
        let mut file = std::fs::File::open(path)?;
        let mut h = Sha256::new();
        let mut buf = vec![0u8; 1 << 16];
        loop {
            match file.read(&mut buf)? {
                0 => return Ok(h.finish()),
                n => h.update(&buf[..n]),
            }
        }
    }
}

impl Default for Sha256 {
    fn default() -> Self {
        Self::new()
    }
}

/// BLAKE2b-256, keyed or not, over data fed in parts.
pub struct Hash(Box<ffi::crypto_generichash_state>);

impl Hash {
    pub fn new(key: Option<&[u8; 32]>) -> Self {
        ready();
        // SAFETY: the state is plain bytes that init fills; it is boxed so it keeps its
        // 64-byte alignment wherever the Hash moves.
        let mut state: Box<ffi::crypto_generichash_state> = Box::new(unsafe { std::mem::zeroed() });
        let (k, klen) = key.map_or((std::ptr::null(), 0), |k| (k.as_ptr(), 32));
        // SAFETY: a 32-byte output, and a 32-byte key or none.
        let rc = unsafe { ffi::crypto_generichash_init(&mut *state, k, klen, 32) };
        assert_eq!(rc, 0, "crypto_generichash_init failed");
        Hash(state)
    }

    pub fn update(&mut self, data: &[u8]) {
        // SAFETY: the state was initialised by new.
        unsafe { ffi::crypto_generichash_update(&mut *self.0, data.as_ptr(), data.len() as u64) };
    }

    pub fn finish(mut self) -> [u8; 32] {
        let mut out = [0u8; 32];
        // SAFETY: the 32-byte output the state was initialised for.
        unsafe { ffi::crypto_generichash_final(&mut *self.0, out.as_mut_ptr(), 32) };
        out
    }

    pub fn of(parts: &[&[u8]]) -> [u8; 32] {
        let mut h = Hash::new(None);
        for p in parts {
            h.update(p);
        }
        h.finish()
    }
}

impl Drop for Hash {
    fn drop(&mut self) {
        wipe(&mut *self.0);
    }
}

/// HMAC-SHA-256 over data fed in parts.
pub struct Hmac(Box<ffi::crypto_auth_hmacsha256_state>);

impl Hmac {
    pub fn new(key: &[u8; 32]) -> Self {
        ready();
        // SAFETY: the state is plain bytes that init fills.
        let mut state: Box<ffi::crypto_auth_hmacsha256_state> =
            Box::new(unsafe { std::mem::zeroed() });
        // SAFETY: a 32-byte key.
        let rc = unsafe { ffi::crypto_auth_hmacsha256_init(&mut *state, key.as_ptr(), 32) };
        assert_eq!(rc, 0, "crypto_auth_hmacsha256_init failed");
        Hmac(state)
    }

    pub fn update(&mut self, data: &[u8]) {
        // SAFETY: the state was initialised by new.
        unsafe {
            ffi::crypto_auth_hmacsha256_update(&mut *self.0, data.as_ptr(), data.len() as u64)
        };
    }

    pub fn finish(mut self) -> [u8; 32] {
        let mut out = [0u8; 32];
        // SAFETY: a 32-byte tag.
        unsafe { ffi::crypto_auth_hmacsha256_final(&mut *self.0, out.as_mut_ptr()) };
        out
    }
}

impl Drop for Hmac {
    fn drop(&mut self) {
        wipe(&mut *self.0);
    }
}

fn wipe<T>(value: &mut T) {
    // SAFETY: overwrites exactly the value's own bytes; every type wiped here is plain data.
    unsafe { ffi::sodium_memzero((value as *mut T).cast(), std::mem::size_of::<T>()) };
}

/// Constant-time comparison of two equal-length secrets or tags.
pub fn equal(a: &[u8], b: &[u8]) -> bool {
    ready();
    // SAFETY: both buffers are a.len() bytes long.
    a.len() == b.len()
        && unsafe { ffi::sodium_memcmp(a.as_ptr().cast(), b.as_ptr().cast(), a.len()) } == 0
}

/// XChaCha20-Poly1305 secretstream, the encrypting side.
pub struct Push(Box<ffi::crypto_secretstream_xchacha20poly1305_state>);

impl Push {
    /// The stream and the header the reader needs to open it.
    pub fn new(key: &[u8; 32]) -> (Self, [u8; STREAM_HEADER]) {
        ready();
        // SAFETY: the state is plain bytes that init fills.
        let mut state: Box<ffi::crypto_secretstream_xchacha20poly1305_state> =
            Box::new(unsafe { std::mem::zeroed() });
        let mut header = [0u8; STREAM_HEADER];
        // SAFETY: a header buffer of HEADERBYTES and a 32-byte key.
        let rc = unsafe {
            ffi::crypto_secretstream_xchacha20poly1305_init_push(
                &mut *state,
                header.as_mut_ptr(),
                key.as_ptr(),
            )
        };
        assert_eq!(rc, 0, "secretstream init_push failed");
        (Push(state), header)
    }

    /// One chunk; the last one of the stream is `last`.
    pub fn push(&mut self, chunk: &[u8], last: bool) -> Vec<u8> {
        let mut out = vec![0u8; chunk.len() + STREAM_ABYTES];
        let tag = if last { TAG_FINAL } else { TAG_MESSAGE };
        // SAFETY: the output holds the chunk plus ABYTES.
        let rc = unsafe {
            ffi::crypto_secretstream_xchacha20poly1305_push(
                &mut *self.0,
                out.as_mut_ptr(),
                std::ptr::null_mut(),
                chunk.as_ptr(),
                chunk.len() as u64,
                std::ptr::null(),
                0,
                tag,
            )
        };
        assert_eq!(rc, 0, "secretstream push failed");
        out
    }
}

impl Drop for Push {
    fn drop(&mut self) {
        wipe(&mut *self.0);
    }
}

/// XChaCha20-Poly1305 secretstream, the decrypting side.
pub struct Pull(Box<ffi::crypto_secretstream_xchacha20poly1305_state>);

impl Pull {
    pub fn new(key: &[u8; 32], header: &[u8; STREAM_HEADER]) -> Result<Self, String> {
        ready();
        // SAFETY: the state is plain bytes that init fills.
        let mut state: Box<ffi::crypto_secretstream_xchacha20poly1305_state> =
            Box::new(unsafe { std::mem::zeroed() });
        // SAFETY: a header of HEADERBYTES and a 32-byte key.
        let rc = unsafe {
            ffi::crypto_secretstream_xchacha20poly1305_init_pull(
                &mut *state,
                header.as_ptr(),
                key.as_ptr(),
            )
        };
        if rc != 0 {
            return Err("the stream header is damaged".into());
        }
        Ok(Pull(state))
    }

    /// One chunk's plaintext and whether it was marked last.
    pub fn pull(&mut self, chunk: &[u8]) -> Result<(Zeroizing<Vec<u8>>, bool), String> {
        if chunk.len() < STREAM_ABYTES {
            return Err("a chunk too short to hold anything".into());
        }
        let mut out = Zeroizing::new(vec![0u8; chunk.len() - STREAM_ABYTES]);
        let mut tag = 0u8;
        // SAFETY: the output is the chunk less ABYTES.
        let rc = unsafe {
            ffi::crypto_secretstream_xchacha20poly1305_pull(
                &mut *self.0,
                out.as_mut_ptr(),
                std::ptr::null_mut(),
                &mut tag,
                chunk.as_ptr(),
                chunk.len() as u64,
                std::ptr::null(),
                0,
            )
        };
        if rc != 0 {
            return Err(
                "a chunk failed its check: changed, reordered or from another stream".into(),
            );
        }
        match tag {
            TAG_MESSAGE => Ok((out, false)),
            TAG_FINAL => Ok((out, true)),
            _ => Err("a chunk with a tag this format never writes".into()),
        }
    }
}

impl Drop for Pull {
    fn drop(&mut self) {
        wipe(&mut *self.0);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn unhex<const N: usize>(s: &str) -> [u8; N] {
        let mut out = [0u8; N];
        hex::decode_to_slice(s, &mut out).unwrap();
        out
    }

    #[test]
    fn hashes_match_their_published_test_vectors() {
        // SHA-256("abc"), FIPS 180-2
        assert_eq!(
            sha256(b"abc"),
            unhex("ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
        );
        // BLAKE2b-256(""), RFC 7693 parameters
        assert_eq!(
            Hash::of(&[]),
            unhex("0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8")
        );
        // HMAC-SHA-256 with 32-byte keys, as Python's hmac module computes it
        assert_eq!(
            Hmac::new(&[0u8; 32]).finish(),
            unhex("b613679a0814d9ec772f95d778c35fc5ff1697c493715653c6c712144292c5ad")
        );
        let key: [u8; 32] = std::array::from_fn(|i| i as u8);
        let mut h = Hmac::new(&key);
        h.update(b"Hi ");
        h.update(b"There");
        assert_eq!(
            h.finish(),
            unhex("278639ec02309d3afded1b273f1349ba63b9089c12476d716bee3ecc94673e9e")
        );
    }

    #[test]
    fn kdf_keys_differ_by_number_and_context() {
        // Each id and context gives a different key; the same ones give the same key.
        let master = [7u8; 32];
        assert_eq!(*kdf(&master, 1, b"fi-test1"), *kdf(&master, 1, b"fi-test1"));
        assert_ne!(*kdf(&master, 1, b"fi-test1"), *kdf(&master, 2, b"fi-test1"));
        assert_ne!(*kdf(&master, 1, b"fi-test1"), *kdf(&master, 1, b"fi-test2"));
    }

    #[test]
    fn a_stream_round_trips_and_refuses_any_change() {
        let key = random::<32>();
        let (mut push, header) = Push::new(&key);
        let a = push.push(b"first", false);
        let b = push.push(b"second", true);
        let mut pull = Pull::new(&key, &header).unwrap();
        assert_eq!((pull.pull(&a).unwrap().0.as_slice(), false).0, b"first");
        let (m, last) = pull.pull(&b).unwrap();
        assert_eq!((m.as_slice(), last), (&b"second"[..], true));
        // reordered
        let mut pull = Pull::new(&key, &header).unwrap();
        assert!(pull.pull(&b).is_err());
        // a flipped byte
        let mut bad = a.clone();
        bad[3] ^= 1;
        assert!(Pull::new(&key, &header).unwrap().pull(&bad).is_err());
        // another key
        let other = random::<32>();
        assert!(Pull::new(&other, &header).unwrap().pull(&a).is_err());
    }

    #[test]
    fn boxes_open_only_for_their_key() {
        let key = random::<32>();
        let sealed = secretbox_seal(&key, b"fleet key");
        assert_eq!(
            secretbox_open(&key, &sealed).unwrap().as_slice(),
            b"fleet key"
        );
        assert!(secretbox_open(&random::<32>(), &sealed).is_err());
        let mut bad = sealed.clone();
        *bad.last_mut().unwrap() ^= 1;
        assert!(secretbox_open(&key, &bad).is_err());

        let secret = x25519_dalek::StaticSecret::from(*random::<32>());
        let public = x25519_dalek::PublicKey::from(&secret).to_bytes();
        let sealed = seal(&public, b"for one laptop");
        let opened = seal_open(&public, &secret.to_bytes(), &sealed).unwrap();
        assert_eq!(opened.as_slice(), b"for one laptop");
        let stranger = x25519_dalek::StaticSecret::from(*random::<32>());
        let theirs = x25519_dalek::PublicKey::from(&stranger).to_bytes();
        assert!(seal_open(&theirs, &stranger.to_bytes(), &sealed).is_err());
    }

    #[test]
    fn sha256_in_parts_is_sha256() {
        let mut h = Sha256::new();
        h.update(b"a");
        h.update(b"bc");
        assert_eq!(h.finish(), sha256(b"abc"));
    }

    #[test]
    fn comparison_is_exact() {
        assert!(equal(b"abc", b"abc"));
        assert!(!equal(b"abc", b"abd"));
        assert!(!equal(b"abc", b"ab"));
    }
}
