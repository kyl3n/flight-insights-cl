//! Sync between laptops, the shell's half (spec: Shared storage and sync). The engine
//! turns records into plaintext objects and applies the ones that pass; the shell
//! encrypts, signs and checks them (`object`), holds the fleet's keys and its device
//! registry (`keyring`), and moves objects to and from the drive.

pub mod drive;
pub mod keyring;
pub mod object;
pub mod run;
pub mod sodium;
