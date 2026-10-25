"""Non-interactive smoke check for the worker's cryptography runtime."""

import json
import os
import sys
from importlib.metadata import version

from Crypto.Cipher import AES
import ecdsa
import gmpy2
import numpy as np
import scipy
import scipy.linalg
import sympy
import z3
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


EXPECTED_PACKAGES = {
    "pycryptodome": "3.24.1",
    "sympy": "1.14.0",
    "mpmath": "1.3.0",
    "gmpy2": "2.3.2",
    "z3-solver": "4.15.4.0",
    "numpy": "2.5.4",
    "scipy": "1.18.1",
    "ecdsa": "0.19.2",
    "cryptography": "50.0.2",
}


def require(condition: bool, check: str) -> None:
    if not condition:
        raise RuntimeError(f"crypto self-check failed: {check}")


def main() -> None:
    require(sys.executable == "/usr/bin/python3", "interpreter")
    installed = {name: version(name) for name in EXPECTED_PACKAGES}
    require(installed == EXPECTED_PACKAGES, "package versions")

    # Small RSA arithmetic and exact integer roots; no floating-point values.
    require(pow(7, 3, 55) == 13, "modular exponentiation")
    require(pow(13, 27, 55) == 7, "RSA round trip")
    require(sympy.integer_nthroot(27, 3) == (3, True), "sympy exact root")
    require(sympy.integer_nthroot(28, 3) == (3, False), "sympy inexact root")
    require(gmpy2.iroot(27, 3) == (3, True), "gmpy2 exact root")
    require(gmpy2.iroot(28, 3) == (3, False), "gmpy2 inexact root")

    # AES-128 known-answer vector from FIPS 197.
    key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
    plaintext = bytes.fromhex("00112233445566778899aabbccddeeff")
    ciphertext = bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")
    cipher = AES.new(key, AES.MODE_ECB)
    require(cipher.encrypt(plaintext) == ciphertext, "AES encrypt vector")
    require(cipher.decrypt(ciphertext) == plaintext, "AES decrypt vector")

    x = z3.Int("x")
    solver = z3.Solver()
    solver.add(x > 2, x < 5, x == 3)
    require(solver.check() == z3.sat, "Z3 satisfiable")
    require(solver.model().eval(x).as_long() == 3, "Z3 model")
    solver.add(x == 4)
    require(solver.check() == z3.unsat, "Z3 unsatisfiable")

    # Vectorised GF(2)/bitset helpers used by stream-cipher and MT-style tasks.
    left = np.array([1, 2, 3], dtype=np.uint64)
    right = np.array([3, 2, 1], dtype=np.uint64)
    require(np.array_equal(left ^ right, np.array([2, 0, 2], dtype=np.uint64)), "numpy xor")
    require(np.packbits(np.array([1, 0, 1, 0, 1, 0, 1, 0], dtype=np.uint8)).tolist() == [0xAA],
            "numpy packbits")
    require(abs(scipy.linalg.det(np.eye(3)) - 1.0) < 1e-9, "scipy determinant")

    signing_key = ecdsa.SigningKey.from_secret_exponent(1, curve=ecdsa.SECP256k1)
    require(len(signing_key.get_verifying_key().to_string()) == 64, "ecdsa key")

    nonce = bytes(12)
    sealed = AESGCM(bytes(16)).encrypt(nonce, b"check", None)
    require(AESGCM(bytes(16)).decrypt(nonce, sealed, None) == b"check", "cryptography AESGCM")

    print(json.dumps({
        "status": "ok",
        "python": sys.version.split()[0],
        "interpreter": sys.executable,
        "uid": os.getuid(),
        "packages": installed,
        "checks": ["import", "modular_power", "integer_roots", "aes_128_vector", "z3_sat_unsat",
                   "numpy_bits", "scipy_det", "ecdsa_key", "cryptography_aesgcm"],
    }, sort_keys=True))


if __name__ == "__main__":
    main()
