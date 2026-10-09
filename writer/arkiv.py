"""Small Arkiv client over plain JSON-RPC (no SDK).

Writes are ordinary signed EIP-1559 transactions to the entity registry 0x44..44 whose calldata is
execute((uint8,bytes)[]). Each op is a tag plus the ABI encoding of that op's struct. Reads use the
node's arkiv_query / arkiv_getEntity methods.

Wire facts this file relies on (Arkiv-Network/arkiv, crates/arkiv-bindings):
  op tags      create 1, patch 2, extend 3, transfer 4, delete 5
  Create       (uint128 salt, uint64 expiresAt, uint64 minLifetime, uint8 creationFlags, Attribute[])
  Attribute    (bytes32 name, uint8 typeId, bytes value), names sorted ascending
  type ids     u64 3 (one right-aligned 32-byte word), bytes 7, str 8 (raw UTF-8)
  flags        readonly 1, permissionless extension 2
  entity key   keccak256(uint256 chainId | registry | owner | uint64 entityNonce | uint128 salt)
  expiry       block numbers; resolved as max(expiresAt, inclusion block + minLifetime)
"""
from __future__ import annotations

import json
import os
import re
import secrets
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode
from eth_account import Account
from eth_utils import keccak, to_checksum_address

CHAIN_ID = 7738577
RPC_URL = os.environ.get("ARKIV_RPC", "https://rpc.tiramisu.db-chain.testnet.arkiv.network")
REGISTRY = "0x4400000000000000000000000000000000000044"
BURN = "0x000000000000000000000000000000000000dEaD"

SEL_EXECUTE = bytes.fromhex("49650044")       # execute((uint8,bytes)[])
SEL_ENTITY_NONCE = bytes.fromhex("36917bfd")  # entityNonce(address)

OP_CREATE, OP_PATCH, OP_EXTEND, OP_TRANSFER, OP_DELETE = 1, 2, 3, 4, 5
T_U64, T_BYTES, T_STR = 3, 7, 8
READONLY, PERMISSIONLESS_EXTENSION = 1, 2

TOPIC_CREATED = "0x" + keccak(b"EntityCreated(bytes32,address,uint64,uint8)").hex()
TOPIC_DELETED = "0x" + keccak(b"EntityDeleted(bytes32,address)").hex()
TOPIC_EXTENDED = "0x" + keccak(b"ExpiryExtended(bytes32,address,uint64)").hex()
TOPIC_TRANSFERRED = "0x" + keccak(b"OwnershipTransferred(bytes32,address,address)").hex()

_NAME_RE = re.compile(r"^[a-z][a-z0-9._-]{0,31}$")
# Words the query language reserves (type tags and keywords): they can be written but never queried.
_RESERVED = {"i32", "u64", "u256", "dec", "str", "addr", "key", "bytes32", "bool",
             "and", "or", "not", "true", "false", "startswith", "exists", "typeof"}

# Custom errors the registry returns through eth_estimateGas / eth_call (selector -> name).
_ERRORS = {
    "0x" + keccak(sig.encode()).hex()[:8]: sig.split("(")[0]
    for sig in (
        "AttributesNotSorted()", "EmptyBatch()", "EmptyMutations(bytes32)", "EntityExpired(bytes32,uint64)",
        "EntityNotFound(bytes32)", "ExpiryNotExtended(bytes32,uint64,uint64)", "ExpiryDeadOnArrival(uint64,uint64)",
        "InvalidOpType(uint8)", "InvalidValueType(bytes32,uint8)", "NonCanonicalOperationData(uint8)",
        "NotOwner(bytes32,address,address)", "ReadOnlyEntity(bytes32)", "ReservedCreationFlags(uint8)",
        "SystemAttributeNotWritable(bytes32)", "TooManyAttributes(uint256,uint256)", "TransferToSelf(bytes32)",
        "TransferToZeroAddress(bytes32)", "Ident32Empty()", "Ident32InvalidByte(uint256,bytes1)",
    )
}


class ArkivError(RuntimeError):
    """An RPC error, or a registry revert decoded to its custom error name when known."""


# ---------------------------------------------------------------------------------------------
# Attributes and ops
# ---------------------------------------------------------------------------------------------

def _name32(name: str) -> bytes:
    if not name.startswith("$") and (not _NAME_RE.match(name) or name in _RESERVED):
        raise ValueError("attribute name %r is not queryable (lowercase, not a reserved word)" % name)
    raw = name.encode("ascii")
    if len(raw) > 32:
        raise ValueError("attribute name %r longer than 32 bytes" % name)
    return raw.ljust(32, b"\0")


def u64(name: str, value: int) -> tuple:
    if not 0 <= value < 2**64:
        raise ValueError("%s=%r is not a u64" % (name, value))
    return (_name32(name), T_U64, value.to_bytes(32, "big"))


def text(name: str, value: str) -> tuple:
    return (_name32(name), T_STR, value.encode("utf-8"))


def payload(data: dict) -> list[tuple]:
    body = json.dumps(data, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return [(_name32("$contentType"), T_STR, b"application/json"), (_name32("$payload"), T_BYTES, body)]


def _sorted(attrs: list[tuple]) -> list[tuple]:
    out = sorted(attrs, key=lambda a: a[0])
    names = [a[0] for a in out]
    if len(set(names)) != len(names):
        raise ValueError("duplicate attribute name")
    return out


@dataclass
class Op:
    tag: int
    data: bytes
    note: str = ""


def op_create(attrs: list[tuple], min_lifetime: int, flags: int = 0, salt: int | None = None,
              expires_at: int = 0) -> tuple[Op, int]:
    """Returns the op and the salt used (needed to predict the key)."""
    salt = secrets.randbits(128) if salt is None else salt
    data = abi_encode(["(uint128,uint64,uint64,uint8,(bytes32,uint8,bytes)[])"],
                      [(salt, expires_at, min_lifetime, flags, _sorted(attrs))])
    return Op(OP_CREATE, data, "create"), salt


def op_extend(key: str, min_lifetime: int, expires_at: int = 0) -> Op:
    return Op(OP_EXTEND, abi_encode(["(bytes32,uint64,uint64)"], [(bytes.fromhex(key[2:]), expires_at, min_lifetime)]),
              "extend")


def op_transfer(key: str, new_owner: str) -> Op:
    return Op(OP_TRANSFER, abi_encode(["(bytes32,address)"], [(bytes.fromhex(key[2:]), to_checksum_address(new_owner))]),
              "transfer")


def op_delete(key: str) -> Op:
    return Op(OP_DELETE, abi_encode(["(bytes32)"], [(bytes.fromhex(key[2:]),)]), "delete")


def execute_calldata(ops: list[Op]) -> str:
    if not ops:
        raise ValueError("empty batch")
    return "0x" + (SEL_EXECUTE + abi_encode(["(uint8,bytes)[]"], [[(o.tag, o.data) for o in ops]])).hex()


def entity_key(owner: str, nonce: int, salt: int, chain_id: int = CHAIN_ID) -> str:
    raw = (chain_id.to_bytes(32, "big") + bytes.fromhex(REGISTRY[2:]) + bytes.fromhex(owner[2:].lower())
           + nonce.to_bytes(8, "big") + salt.to_bytes(16, "big"))
    return "0x" + keccak(raw).hex()


# ---------------------------------------------------------------------------------------------
# RPC
# ---------------------------------------------------------------------------------------------

@dataclass
class Rpc:
    url: str = RPC_URL
    timeout: float = 30.0
    retries: int = 4
    _id: int = field(default=0, repr=False)

    def call(self, method: str, params: list):
        delay = 2.0
        for attempt in range(self.retries + 1):
            self._id += 1
            body = json.dumps({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}).encode()
            req = urllib.request.Request(self.url, body, {"Content-Type": "application/json",
                                                           "User-Agent": "door-ledger/0.1"})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    out = json.loads(r.read())
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503, 504) and attempt < self.retries:
                    wait = float(e.headers.get("Retry-After") or delay)
                    time.sleep(min(wait, 120.0))
                    delay *= 2
                    continue
                raise ArkivError("%s HTTP %s" % (method, e.code)) from None
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
                if attempt < self.retries:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise ArkivError("%s failed: %s" % (method, e)) from None
            if "error" in out:
                raise ArkivError(_explain(method, out["error"]))
            return out["result"]
        raise ArkivError("%s: retries exhausted" % method)

    # chain
    def block_number(self) -> int:
        return int(self.call("eth_blockNumber", []), 16)

    def balance(self, addr: str) -> int:
        return int(self.call("eth_getBalance", [addr, "latest"]), 16)

    def entity_nonce(self, owner: str) -> int:
        data = "0x" + (SEL_ENTITY_NONCE + abi_encode(["address"], [to_checksum_address(owner)])).hex()
        return int(self.call("eth_call", [{"to": REGISTRY, "data": data}, "latest"]), 16)

    # arkiv reads
    def query(self, q: str, at_block: int | None = None, limit: int = 100, cursor: str | None = None,
              select: dict | None = None) -> dict:
        opts: dict = {"limit": limit,
                      "select": select or {"key": True, "creator": True, "owner": True, "createdAt": True,
                                           "expiresAt": True, "attributes": True, "payload": True}}
        if at_block is not None:
            opts["atBlock"] = hex(at_block)
        if cursor:
            opts["cursor"] = cursor
        return self.call("arkiv_query", [q, opts])

    def get_entity(self, key: str):
        return self.call("arkiv_getEntity", [key])


def _explain(method: str, err: dict) -> str:
    data = err.get("data")
    if isinstance(data, str) and data.startswith("0x") and len(data) >= 10:
        name = _ERRORS.get(data[:10].lower())
        if name:
            return "%s reverted: %s (%s)" % (method, name, data)
    return "%s error %s: %s" % (method, err.get("code"), err.get("message"))


# ---------------------------------------------------------------------------------------------
# Signing and sending (one transaction in flight per wallet)
# ---------------------------------------------------------------------------------------------

def load_account(role: str, path: str | None = None):
    """Read ARKIV_SIGNER_HEX from ~/.arkiv-<role>.env (or ARKIV_KEY_FILE). The key is never printed."""
    path = path or os.environ.get("ARKIV_KEY_FILE") or os.path.join(os.path.expanduser("~"), ".arkiv-%s.env" % role)
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith("ARKIV_SIGNER_HEX="):
                try:
                    return Account.from_key(line.split("=", 1)[1].strip())
                except Exception:  # noqa: BLE001 - never echo the exception text (it may carry the key)
                    raise ArkivError("key in %s is not a valid private key" % path) from None
    raise ArkivError("no ARKIV_SIGNER_HEX line in %s" % path)


@dataclass
class Receipt:
    tx: str
    block: int
    gas_used: int
    created: list[str]
    logs: list[dict]


class Writer:
    def __init__(self, account, rpc: Rpc | None = None, log=print):
        self.acct = account
        self.addr = account.address
        self.rpc = rpc or Rpc()
        self.log = log

    def simulate(self, ops: list[Op]) -> int:
        data = execute_calldata(ops)
        return int(self.rpc.call("eth_estimateGas", [{"from": self.addr, "to": REGISTRY, "data": data}]), 16)

    def send(self, ops: list[Op], wait_s: float = 90.0) -> Receipt:
        data = execute_calldata(ops)
        gas = self.simulate(ops)  # raises the decoded revert before anything is signed
        nonce = int(self.rpc.call("eth_getTransactionCount", [self.addr, "pending"]), 16)
        base = int(self.rpc.call("eth_gasPrice", []), 16)
        tx = {"type": 2, "chainId": CHAIN_ID, "nonce": nonce, "to": REGISTRY, "value": 0, "data": data,
              "gas": int(gas * 1.15) + 5000, "maxFeePerGas": max(2 * base, 2 * 10**9), "maxPriorityFeePerGas": 2}
        signed = self.acct.sign_transaction(tx)
        raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
        h = self.rpc.call("eth_sendRawTransaction", ["0x" + bytes(raw).hex()])
        deadline = time.time() + wait_s
        while time.time() < deadline:
            r = self.rpc.call("eth_getTransactionReceipt", [h])
            if r:
                if r["status"] != "0x1":
                    raise ArkivError("tx %s reverted on chain" % h)
                created = [lg["topics"][1] for lg in r["logs"]
                           if lg["address"].lower() == REGISTRY.lower() and lg["topics"][0] == TOPIC_CREATED]
                return Receipt(h, int(r["blockNumber"], 16), int(r["gasUsed"], 16), created, r["logs"])
            time.sleep(2)
        raise ArkivError("no receipt for %s after %ss (tx may still land; re-read state before retrying)" % (h, wait_s))


def attrs_of(entity: dict) -> dict:
    """Flatten an arkiv_query row's attributes ([{name, type, value}]) into {name: value}; u64 as int."""
    out = {}
    for a in entity.get("attributes") or []:
        v = a.get("value")
        if a.get("type") == "u64" and isinstance(v, str):
            v = int(v, 16) if v.startswith("0x") else int(v)
        out[a["name"]] = v
    return out


def payload_of(entity: dict) -> dict | None:
    """Decode a row's JSON payload (returned hex-encoded); None when absent or not JSON."""
    p = entity.get("payload")
    if not p:
        return None
    try:
        raw = bytes.fromhex(p[2:]) if isinstance(p, str) and p.startswith("0x") else str(p).encode()
        return json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return None


def decode_revert(data_hex: str) -> str:
    name = _ERRORS.get(data_hex[:10].lower(), "unknown")
    return name


__all__ = [n for n in dir() if not n.startswith("_")] + ["abi_decode"]
