"""Exercise the AES verifier with the S03 worker's pinned PyCryptodome."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from Crypto.Cipher import AES

from echo_agent.crypto_verify import _verify_aes


key = bytes(range(16))
nonce = bytes(range(12))
iv = bytes(range(16))
plaintext = b"flag{known}"
aad = b"challenge"
known_hash = hashlib.sha256(plaintext).hexdigest()

gcm = AES.new(key, AES.MODE_GCM, nonce=nonce, mac_len=16)
gcm.update(aad)
ciphertext, tag = gcm.encrypt_and_digest(plaintext)
manifest = {"algorithm": "aes", "mode": "gcm", "nonce_hex": nonce.hex(),
            "ciphertext_hex": ciphertext.hex(), "tag_hex": tag.hex(),
            "aad_hex": aad.hex(), "known_plaintext_sha256": known_hash}
candidate = {"scheme": "aes", "key_hex": key.hex(), "answer": plaintext.decode()}
assert _verify_aes(json.dumps(manifest).encode(), candidate)[0] == "pass"
manifest["tag_hex"] = "00" * 16
assert _verify_aes(json.dumps(manifest).encode(), candidate)[0] == "fail"

padding = 16 - len(plaintext) % 16
cbc_ciphertext = AES.new(key, AES.MODE_CBC, iv=iv).encrypt(plaintext + bytes([padding]) * padding)
manifest = {"algorithm": "aes", "mode": "cbc", "iv_hex": iv.hex(),
            "ciphertext_hex": cbc_ciphertext.hex(), "padding": "pkcs7",
            "known_plaintext_sha256": known_hash}
assert _verify_aes(json.dumps(manifest).encode(), candidate)[0] == "pass"
manifest["known_plaintext_sha256"] = "0" * 64
assert _verify_aes(json.dumps(manifest).encode(), candidate)[0] == "fail"
manifest["known_plaintext_sha256"] = known_hash
manifest["iv_hex"] = (iv[:-1] + bytes([iv[-1] ^ 1])).hex()
assert _verify_aes(json.dumps(manifest).encode(), candidate)[0] == "fail"
print("AES-GCM/CBC positive and negative checks passed")
