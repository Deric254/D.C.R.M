"""The owner's PUBLIC key: the one thing that makes a copy of the app answer to the owner account.

Every copy of the app checks licences and the owner's identity against this key. Only the matching PRIVATE key,
which only the owner holds, can make a licence these copies accept. A public key is safe to publish.

Set it once, before you build the installer:
    python scripts/make-owner-key.py        (makes dericbi-owner.key: keep it secret; prints the line below)
then paste the printed value here.
"""
OWNER_PUBLIC_KEY = "e4c79b36e1f58b5690ac3be0b9c43226eff08d98520c820c4f893d1e56c69d29"   # 64 hex characters, printed by scripts/make-owner-key.py
