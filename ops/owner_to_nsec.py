#!/usr/bin/env python3
"""Local converter — run this YOURSELF in a terminal to get the owner nsec/npub for
importing into the Buzz desktop app. Reads ./owner.key (hex secret) and prints bech32.
Nothing leaves your machine. After importing + saving to your password manager, shred owner.key.

    python3 ~/factory/ops/owner_to_nsec.py
"""
import re, os

FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "owner.key")

CH = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
def convertbits(data, frm, to, pad=True):
    acc=0;bits=0;ret=[];maxv=(1<<to)-1
    for b in data:
        acc=(acc<<frm)|b;bits+=frm
        while bits>=to: bits-=to;ret.append((acc>>bits)&maxv)
    if pad and bits: ret.append((acc<<(to-bits))&maxv)
    return ret
def polymod(v):
    G=[0x3b6a57b2,0x26508e6d,0x1ea119fa,0x3d4233dd,0x2a1462b3];c=1
    for x in v:
        b=c>>25;c=((c&0x1ffffff)<<5)^x
        for i in range(5): c^=G[i] if ((b>>i)&1) else 0
    return c
def hrp_expand(h): return [ord(x)>>5 for x in h]+[0]+[ord(x)&31 for x in h]
def checksum(h,d):
    pm=polymod(hrp_expand(h)+d+[0]*6)^1
    return [(pm>>5*(5-i))&31 for i in range(6)]
def bech(h,hx):
    d=convertbits(bytes.fromhex(hx),8,5,True); return h+"1"+"".join(CH[x] for x in d+checksum(h,d))

P=0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
G=(0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
   0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8)
def add(a,b):
    if a is None: return b
    if b is None: return a
    if a[0]==b[0] and a[1]!=b[1]: return None
    l=(3*a[0]*a[0]*pow(2*a[1],P-2,P))%P if a==b else ((b[1]-a[1])*pow(b[0]-a[0],P-2,P))%P
    x=(l*l-a[0]-b[0])%P
    return (x,(l*(a[0]-x)-a[1])%P)
def mul(k,pt=G):
    r=None
    while k:
        if k&1: r=add(r,pt)
        pt=add(pt,pt);k>>=1
    return r

raw = open(FILE).read()
m = re.search(r"Secret key:\s*([0-9a-fA-F]{64})", raw)
sk = m.group(1).lower() if m else (re.findall(r"(?<![0-9a-fA-F])[0-9a-fA-F]{64}(?![0-9a-fA-F])", raw) or [None])[-1]
if not sk:
    raise SystemExit("No 64-char hex secret found in owner.key")
pub = "%064x" % mul(int(sk,16))[0]

print("owner npub (public) :", bech("npub", pub))
print("owner nsec (SECRET) :", bech("nsec", sk))
print("owner hex  (SECRET) :", sk)
print("\nImport the nsec into the Buzz desktop app, save it to your password manager,")
print("then destroy this file:  shred -u", FILE, " (or delete it).")
