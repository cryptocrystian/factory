"""Notifier port — Buzz adapter.

A dependency-free client for the Buzz relay's HTTP bridge (POST /events, /query,
/count) authenticated with NIP-98 Schnorr request signatures. Buzz is a Nostr
relay (NIP-29 groups); the operator agent's keypair *is* the credential.

Pure-Python secp256k1 + BIP-340 (adapted from the BIP-340 reference impl, public
domain) so the factory can talk to Buzz with nothing but the stdlib. Occasional
low-volume signing — pure Python is plenty fast.

Env (from ~/.config/buzz/operator-agent.env):
  BUZZ_RELAY_URL   e.g. https://buzz.saipienlabs.com  (wss:// is normalized)
  BUZZ_PRIVATE_KEY 64-char hex secp256k1 secret key
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.request
import urllib.error
import base64
import uuid
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# secp256k1 + BIP-340 Schnorr (reference implementation, public domain)
# ---------------------------------------------------------------------------
_p = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
_n = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_G = (0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
      0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8)


def _tagged_hash(tag: str, msg: bytes) -> bytes:
    th = hashlib.sha256(tag.encode()).digest()
    return hashlib.sha256(th + th + msg).digest()


def _point_add(P1, P2):
    if P1 is None:
        return P2
    if P2 is None:
        return P1
    if P1[0] == P2[0] and (P1[1] != P2[1]):
        return None
    if P1 == P2:
        lam = (3 * P1[0] * P1[0] * pow(2 * P1[1], _p - 2, _p)) % _p
    else:
        lam = ((P2[1] - P1[1]) * pow(P2[0] - P1[0], _p - 2, _p)) % _p
    x3 = (lam * lam - P1[0] - P2[0]) % _p
    return (x3, (lam * (P1[0] - x3) - P1[1]) % _p)


def _point_mul(P, k):
    R = None
    for i in range(256):
        if (k >> i) & 1:
            R = _point_add(R, P)
        P = _point_add(P, P)
    return R


def _has_even_y(P) -> bool:
    return P[1] % 2 == 0


def _bytes_from_int(x: int) -> bytes:
    return x.to_bytes(32, "big")


def _lift_x(x: int):
    if x >= _p:
        return None
    y_sq = (pow(x, 3, _p) + 7) % _p
    y = pow(y_sq, (_p + 1) // 4, _p)
    if pow(y, 2, _p) != y_sq:
        return None
    return (x, y if y % 2 == 0 else _p - y)


def pubkey_xonly(seckey: bytes) -> bytes:
    """32-byte x-only public key for a 32-byte secret key."""
    d0 = int.from_bytes(seckey, "big")
    if not (1 <= d0 <= _n - 1):
        raise ValueError("secret key out of range")
    P = _point_mul(_G, d0)
    return _bytes_from_int(P[0])


def schnorr_sign(msg32: bytes, seckey: bytes, aux_rand: bytes) -> bytes:
    d0 = int.from_bytes(seckey, "big")
    if not (1 <= d0 <= _n - 1):
        raise ValueError("secret key out of range")
    P = _point_mul(_G, d0)
    d = d0 if _has_even_y(P) else _n - d0
    t = (d ^ int.from_bytes(_tagged_hash("BIP0340/aux", aux_rand), "big"))
    t_bytes = _bytes_from_int(t)
    k0 = int.from_bytes(
        _tagged_hash("BIP0340/nonce", t_bytes + _bytes_from_int(P[0]) + msg32), "big"
    ) % _n
    if k0 == 0:
        raise RuntimeError("nonce is zero (astronomically unlikely)")
    R = _point_mul(_G, k0)
    k = k0 if _has_even_y(R) else _n - k0
    e = int.from_bytes(
        _tagged_hash("BIP0340/challenge",
                     _bytes_from_int(R[0]) + _bytes_from_int(P[0]) + msg32), "big"
    ) % _n
    sig = _bytes_from_int(R[0]) + _bytes_from_int((k + e * d) % _n)
    return sig


def schnorr_verify(pubkey32: bytes, msg32: bytes, sig: bytes) -> bool:
    """BIP-340 verification — used only for the known-answer self-test (the relay verifies in prod)."""
    if len(pubkey32) != 32 or len(msg32) != 32 or len(sig) != 64:
        return False
    P = _lift_x(int.from_bytes(pubkey32, "big"))
    if P is None:
        return False
    r = int.from_bytes(sig[:32], "big")
    s = int.from_bytes(sig[32:], "big")
    if r >= _p or s >= _n:
        return False
    e = int.from_bytes(_tagged_hash("BIP0340/challenge", sig[:32] + pubkey32 + msg32), "big") % _n
    R = _point_add(_point_mul(_G, s), _point_mul(P, _n - e))
    if R is None or not _has_even_y(R) or R[0] != r:
        return False
    return True


# ---------------------------------------------------------------------------
# NIP-01 event
# ---------------------------------------------------------------------------
def _serialize_for_id(pubkey_hex: str, created_at: int, kind: int, tags, content: str) -> bytes:
    arr = [0, pubkey_hex, created_at, kind, tags, content]
    return json.dumps(arr, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def build_event(seckey: bytes, kind: int, tags, content: str, created_at: int | None = None) -> dict:
    pk = pubkey_xonly(seckey).hex()
    ts = int(time.time()) if created_at is None else created_at
    ser = _serialize_for_id(pk, ts, kind, tags, content)
    eid = hashlib.sha256(ser).hexdigest()
    sig = schnorr_sign(bytes.fromhex(eid), seckey, os.urandom(32)).hex()
    return {"id": eid, "pubkey": pk, "created_at": ts, "kind": kind,
            "tags": tags, "content": content, "sig": sig}


# ---------------------------------------------------------------------------
# Bridge client
# ---------------------------------------------------------------------------
def normalize_relay_url(url: str) -> str:
    return (url.replace("wss://", "https://").replace("ws://", "http://").rstrip("/"))


KIND_STREAM_MESSAGE = 9
KIND_NIP29_CREATE_GROUP = 9007
KIND_ADD_MEMBER = 9000       # NIP-29 add-user to group
KIND_HTTP_AUTH = 27235
KIND_GROUP_METADATA = 39000  # relay-signed channel metadata (d=uuid, name, ...)
KIND_GROUP_MEMBERS = 39002   # relay-signed member list (p tags)


def canonical_channel_name(name: str) -> str:
    """Mirror buzz-core canonical_channel_name: strip leading '#', trim, lowercase,
    collapse internal whitespace to single hyphens. Conservative but stable."""
    n = name.strip().lstrip("#").strip().lower()
    return "-".join(n.split())


@dataclass
class BuzzClient:
    relay_url: str
    seckey: bytes

    @classmethod
    def from_env(cls, env_path: str | None = None) -> "BuzzClient":
        relay = os.environ.get("BUZZ_RELAY_URL")
        sk = os.environ.get("BUZZ_PRIVATE_KEY")
        if (not relay or not sk) and env_path is None:
            env_path = os.path.expanduser("~/.config/buzz/operator-agent.env")
        if env_path and (not relay or not sk):
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("export "):
                        line = line[len("export "):]
                    if line.startswith("BUZZ_RELAY_URL="):
                        relay = line.split("=", 1)[1].strip().strip('"')
                    elif line.startswith("BUZZ_PRIVATE_KEY="):
                        sk = line.split("=", 1)[1].strip().strip('"')
        if not relay or not sk:
            raise RuntimeError("BUZZ_RELAY_URL / BUZZ_PRIVATE_KEY not found")
        return cls(normalize_relay_url(relay), bytes.fromhex(sk))

    @property
    def pubkey(self) -> str:
        return pubkey_xonly(self.seckey).hex()

    def _nip98_header(self, method: str, url: str, body: bytes | None) -> str:
        tags = [["u", url], ["method", method], ["nonce", str(uuid.uuid4())]]
        if body is not None:
            tags.append(["payload", hashlib.sha256(body).hexdigest()])
        ev = build_event(self.seckey, KIND_HTTP_AUTH, tags, "")
        return "Nostr " + base64.b64encode(json.dumps(ev).encode()).decode()

    def _post(self, path: str, payload) -> object:
        url = f"{self.relay_url}{path}"
        body = json.dumps(payload, separators=(",", ":")).encode()
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Authorization", self._nip98_header("POST", url, body))
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            raise RuntimeError(f"HTTP {e.code} on {path}: {detail}") from None

    # --- public API ---
    def query(self, *filters) -> list:
        """POST /query with one or more Nostr filters (ORed). Returns events."""
        return self._post("/query", list(filters))

    def count(self, filt) -> object:
        return self._post("/count", [filt])

    def publish(self, event: dict) -> object:
        """POST /events with a signed Nostr event."""
        return self._post("/events", event)

    def send_message(self, channel_uuid: str, text: str) -> object:
        ev = build_event(self.seckey, KIND_STREAM_MESSAGE,
                         [["h", channel_uuid]], text)
        return self.publish(ev)

    def read_channel(self, channel_uuid: str, limit: int = 20) -> list:
        return self.query({"kinds": [KIND_STREAM_MESSAGE],
                           "#h": [channel_uuid], "limit": limit})

    def create_channel(self, name: str, *, channel_type: str = "stream",
                       visibility: str = "open", about: str | None = None) -> tuple[str, object]:
        """Create a NIP-29 channel (kind 9007). Returns (channel_uuid, relay_response).
        The UUID is generated client-side and becomes the channel's `h`/`d` id."""
        cid = str(uuid.uuid4())
        tags = [["h", cid], ["name", canonical_channel_name(name)],
                ["visibility", visibility], ["channel_type", channel_type]]
        if about:
            tags.append(["about", about])
        ev = build_event(self.seckey, KIND_NIP29_CREATE_GROUP, tags, "")
        return cid, self.publish(ev)

    def add_member(self, channel_uuid: str, pubkey: str, role: str | None = None) -> object:
        """Add a member to a channel (NIP-29 kind 9000). role: 'admin' | None."""
        tags = [["h", channel_uuid], ["p", pubkey.lower()]]
        if role:
            tags.append(["role", role])
        ev = build_event(self.seckey, KIND_ADD_MEMBER, tags, "")
        return self.publish(ev)

    def channel_members(self, channel_uuid: str) -> list:
        """Return member pubkeys for a channel (relay-signed kind 39002)."""
        out = []
        for e in self.query({"kinds": [KIND_GROUP_MEMBERS], "#d": [channel_uuid], "limit": 5}):
            for t in e.get("tags", []):
                if t and t[0] == "p" and len(t) > 1:
                    out.append(t[1])
        return out

    def list_channels(self, limit: int = 500) -> list:
        """List channels via relay-signed group metadata (kind 39000).
        Returns [{channel_id, name}]."""
        out = []
        for e in self.query({"kinds": [KIND_GROUP_METADATA], "limit": limit}):
            cid = name = ""
            for t in e.get("tags", []):
                if t and t[0] == "d" and len(t) > 1:
                    cid = t[1]
                elif t and t[0] == "name" and len(t) > 1:
                    name = t[1]
            if cid:
                out.append({"channel_id": cid, "name": name})
        return out


# ---------------------------------------------------------------------------
# BIP-340 known-answer vectors (official, bitcoin/bips bip-0340/test-vectors.csv).
# (secret_key | None, public_key, aux_rand, message, signature, expected_valid)
# ---------------------------------------------------------------------------
_BIP340_VECTORS = [
    ("0000000000000000000000000000000000000000000000000000000000000003",
     "F9308A019258C31049344F85F89D5229B531C845836F99B08601F113BCE036F9",
     "0000000000000000000000000000000000000000000000000000000000000000",
     "0000000000000000000000000000000000000000000000000000000000000000",
     "E907831F80848D1069A5371B402410364BDF1C5F8307B0084C55F1CE2DCA821525F66A4A85EA8B71E482A74F382D2CE5EBEEE8FDB2172F477DF4900D310536C0",
     True),
    ("B7E151628AED2A6ABF7158809CF4F3C762E7160F38B4DA56A784D9045190CFEF",
     "DFF1D77F2A671C5F36183726DB2341BE58FEAE1DA2DECED843240F7B502BA659",
     "0000000000000000000000000000000000000000000000000000000000000001",
     "243F6A8885A308D313198A2E03707344A4093822299F31D0082EFA98EC4E6C89",
     "6896BD60EEAE296DB48A229FF71DFE071BDE413E6D43F917DC8DCF8C78DE33418906D11AC976ABCCB20B091292BFF4EA897EFCB639EA871CFA95F6DE339E4B0A",
     True),
    ("C90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74020BBEA63B14E5C9",
     "DD308AFEC5777E13121FA72B9CC1B7CC0139715309B086C960E18FD969774EB8",
     "C87AA53824B4D7AE2EB035A2B5BBBCCC080E76CDC6D1692C4B0B62D798E6D906",
     "7E2D58D8B3BCDF1ABADEC7829054F90DDA9805AAB56C77333024B9D0A508B75C",
     "5831AAEED7B44BB74E5EAB94BA9D4294C49BCF2A60728D8B4C200F50DD313C1BAB745879A5AD954A72C45A91C3A51D3C7ADEA98D82F8481E0E1E03674A6F3FB7",
     True),
    ("0B432B2677937381AEF05BB02A66ECD012773062CF3FA2549E44F58ED2401710",
     "25D1DFF95105F5253C4022F628A996AD3A0D95FBF21D468A1B33F8C160D8F517",
     "FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF",
     "FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF",
     "7EB0509757E246F19449885651611CB965ECC1A187DD51B64FDA1EDC9637D5EC97582B9CB13DB3933705B32BA982AF5AF25FD78881EBB32771FC5922EFC66EA3",
     True),
    (None,
     "D69C3509BB99E412E68B0FE8544E72837DFA30746D8BE2AA65975F29D22DC7B9",
     "",
     "4DF3C3F68FCC83B27E9D42C90431A72499F17875C81A599B566C9889B9696703",
     "00000000000000000000003B78CE563F89A0ED9414F5AA28AD0D96D6795F9C6376AFB1548AF603B3EB45C9F8207DEE1060CB71C04E80F593060B07D28308D7F4",
     True),
]


def cryptotest() -> bool:
    """Known-answer self-test for the pure-Python schnorr/BIP-340 implementation.
    Verifies pubkey derivation, deterministic signing, verification, and tamper-rejection
    against the official BIP-340 vectors. (Live interop is separately proven: the relay,
    an independent secp256k1 implementation, accepts our signed events.)"""
    ok = True
    for i, (sk, pk, aux, msg, sig, valid) in enumerate(_BIP340_VECTORS):
        try:
            pkb, msgb, sigb = bytes.fromhex(pk), bytes.fromhex(msg), bytes.fromhex(sig)
            if sk:
                derived = pubkey_xonly(bytes.fromhex(sk)).hex().upper()
                assert derived == pk.upper(), f"pubkey derivation {derived} != {pk}"
                if aux:
                    got = schnorr_sign(msgb, bytes.fromhex(sk), bytes.fromhex(aux)).hex().upper()
                    assert got == sig.upper(), "deterministic sign mismatch"
            v = schnorr_verify(pkb, msgb, sigb)
            assert v == valid, f"verify returned {v}, expected {valid}"
            if valid:                                   # tamper: a flipped message bit must not verify
                bad = bytearray(msgb); bad[0] ^= 0x01
                assert not schnorr_verify(pkb, bytes(bad), sigb), "tampered message still verified"
            print(f"  [PASS] BIP-340 vector {i}"
                  f"{' (derive+sign+verify)' if sk and aux else ' (verify-only)' if not sk else ' (derive+verify)'}")
        except Exception as ex:
            ok = False
            print(f"  [FAIL] BIP-340 vector {i}: {ex}")
    print("buzz.py crypto self-test:", "ALL PASS" if ok else "FAILURES")
    return ok


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "cryptotest":   # pure math, no env/network needed
        sys.exit(0 if cryptotest() else 1)
    c = BuzzClient.from_env()
    print(f"relay={c.relay_url}")
    print(f"pubkey={c.pubkey}")
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        # correctness gate — must match the enrolled operator pubkey
        expect = "cb04d4f3770bd68c707ee722402b574e0c8e167155f13da541d19a88c174ae7a"
        print("MATCH" if c.pubkey == expect else f"MISMATCH (expected {expect})")
