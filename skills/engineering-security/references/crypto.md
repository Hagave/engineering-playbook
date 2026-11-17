# Cryptography

CWE-327, CWE-328, CWE-916, CWE-330, CWE-338, CWE-295, CWE-208, CWE-321

**Load when** the code stores passwords, generates tokens, ids, codes or keys, encrypts or signs data, compares secrets, or configures TLS for an outbound client.

## Invariant

- Passwords are stored with an adaptive password hash.
- Secrets and tokens come from a CSPRNG.
- Encryption is authenticated.
- TLS certificates are always verified.
- Secret comparisons take constant time.
- No algorithm is invented.

## Sinks

- `md5`, `sha1` or `sha256` applied to passwords, with or without a salt.
- `Math.random()`, `random.random()`, `rand()`, timestamps or UUIDv1 used for tokens, reset codes, OTPs, session ids or API keys.
- `createCipheriv` with ECB or CBC and no MAC, a static or reused IV/nonce, keys derived by hashing a passphrase once.
- TLS verification turned off:
  - `rejectUnauthorized: false`, `NODE_TLS_REJECT_UNAUTHORIZED=0`;
  - `verify=False`, `InsecureSkipVerify: true`;
  - trust-all `TrustManager`s.
- `===` / `==` comparison of HMACs, tokens or API keys.
- Keys and salts in source code. Homemade signing or token formats.

## Control

- Passwords: Argon2id through a maintained library. bcrypt is acceptable with a cost of 12 or more; it truncates input at 72 bytes, so pre-hashing or a length limit must be explicit. scrypt is also acceptable.
- Randomness: `crypto.randomBytes`, `crypto.randomUUID`, `secrets.token_urlsafe`, `SecureRandom`. At least 128 bits for tokens.
- Encryption: AES-256-GCM or ChaCha20-Poly1305 with a unique nonce per message, or a high-level library (libsodium secretbox, Tink). Keys come from a KMS or a secret manager, with rotation.
- Comparison: `crypto.timingSafeEqual`, `hmac.compare_digest`, `MessageDigest.isEqual`.
- TLS 1.2+ with verification on. Internal CAs go into the trust store; verification is never disabled.
- Standard formats (JWT via a library, see `tokens-sessions.md`) instead of homemade ones.

## Negative proof

| Check | Expected |
|---|---|
| the stored hash of a new password | starts with `$argon2id$` (or `$2b$12$` or higher) |
| the same password stored twice | two different hashes (salted) |
| 10,000 generated tokens | all unique, each with the expected length and alphabet |
| ciphertext with one byte flipped | decryption throws; no plaintext returned |
| ciphertexts of the same plaintext encrypted twice | different (unique nonce) |
| the outbound client calling a test server with a self-signed certificate | the connection fails |
| a password longer than 72 bytes with bcrypt | the documented behavior (rejected, or pre-hashed), never silently truncated |

## Dynamic proof

Not provable in the lab: the scanner reaches the app over plain HTTP inside the lab network, so no TLS is scanned. TLS is checked where it terminates (proxy, load balancer); hashing and randomness are proven by the tests above.

## Review signal

- An import of `md5`/`sha1` near a password.
- `Math.random` in anything named token, code, key, secret or id.
- A cipher mode string.
- A TLS option set to false.
