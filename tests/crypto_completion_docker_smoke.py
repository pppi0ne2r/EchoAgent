"""Offline verifier check inside the worker image; no model or target access.

Mount echo_agent/ at /src/echo_agent and this file at /check.py, use /src
as workdir, a read-only filesystem, network none and writable /tmp.
"""

import hashlib
import json
import tempfile
from pathlib import Path

from Crypto.Cipher import AES

from echo_agent.crypto_files import CryptoProjectFiles
from echo_agent.crypto_verify import verify_candidate
from echo_agent.models import FactKind, Project


def main():
    with tempfile.TemporaryDirectory() as directory:
        files = CryptoProjectFiles(Path(directory) / "projects")
        inputs, _ = files.ensure_layout(7)
        path = inputs / "aes.json"
        # NIST's all-zero key/nonce/plaintext AES-GCM vector.
        manifest = {"algorithm": "aes", "mode": "gcm", "nonce_hex": "00" * 12,
                    "ciphertext_hex": "0388dace60b6a392f328c2b971b2fe78",
                    "tag_hex": "ab6e47d42cec13bdf53a67b21257bddf"}
        path.write_text(json.dumps(manifest))
        project = Project(id=7, name="offline-aes", task_type="crypto", crypto={
            "schema_version": 1, "inputs": [files.describe_input(7, "input-1", "input", "inputs/aes.json")],
            "goal": {"answer_encoding": "hex"}})
        candidate = project.add_fact("candidate", kind=FactKind.CANDIDATE, crypto={
            "claim_status": "candidate", "source_input_ids": ["input-1"],
            "candidate": {"scheme": "aes", "answer": "00" * 16, "answer_encoding": "hex", "key_hex": "00" * 16}})

        def check(expected):
            path.write_text(json.dumps(manifest))
            project.crypto["inputs"][0] = files.describe_input(7, "input-1", "input", "inputs/aes.json")
            record = verify_candidate(project, candidate.id, files)
            assert record["status"] == expected, record
            return record

        record = check("pass")
        assert record["checks"]["constraint"] == "gcm-authentication-tag"
        assert "plaintext_sha256_matches" not in record["checks"]
        manifest["tag_hex"] = "00" * 16
        check("fail")
        manifest["tag_hex"] = "ab6e47d42cec13bdf53a67b21257bddf"
        candidate.crypto["candidate"]["answer"] = "01" * 16
        check("fail")
        candidate.crypto["candidate"]["answer"] = "00" * 16
        manifest["known_plaintext_sha256"] = "00" * 32
        check("fail")
        manifest["known_plaintext_sha256"] = hashlib.sha256(bytes(16)).hexdigest()
        check("pass")
        plaintext = b"flag{bounded-cbc}"
        padding = 16 - len(plaintext) % 16
        ciphertext = AES.new(bytes(16), AES.MODE_CBC, iv=bytes(16)).encrypt(plaintext + bytes([padding]) * padding)
        manifest = {"algorithm": "aes", "mode": "cbc", "iv_hex": "00" * 16,
                    "ciphertext_hex": ciphertext.hex(), "padding": "pkcs7"}
        project.crypto["goal"] = {"answer_encoding": "utf8"}
        candidate.crypto["candidate"].update(answer=plaintext.decode(), answer_encoding="utf8")
        check("inconclusive")
        manifest["known_plaintext_sha256"] = hashlib.sha256(plaintext).hexdigest()
        check("pass")
        manifest["known_plaintext_sha256"] = "00" * 32
        check("fail")
        project.crypto["goal"] = {"answer_kind": "factors"}
        check("fail")
        print(json.dumps({"ok": True, "checks": 9, "backend": "pycryptodome", "network": "none"}))


if __name__ == "__main__":
    main()
