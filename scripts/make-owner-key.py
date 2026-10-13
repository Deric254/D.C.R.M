"""Make YOUR owner key. Run this once, on your own computer.

It writes dericbi-owner.key (the PRIVATE key: whoever has this file can authorise computers and create the owner account,
so keep it off GitHub, off WhatsApp, on a USB stick or in a password manager) and prints the PUBLIC key line to paste into
backend/owner_key.py before you build the installer. The public key is safe to publish; it can only check, never sign.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "backend"))
import auth  # noqa: E402

target = pathlib.Path("dericbi-owner.key")
if target.exists():
    sys.exit(f"{target} already exists. Move it somewhere safe first: a new key would stop every licence you have issued working.")
key, public = auth.make_owner_key()
target.write_text(key + "\n", encoding="utf-8")
print(f"""
Made {target.resolve()}
  KEEP THIS FILE SECRET AND MAKE A BACKUP. If it is lost, no new licences can be issued.

Now open backend/owner_key.py and set:

    OWNER_PUBLIC_KEY = "{public}"

then build the installer. Also add dericbi-owner.key to .gitignore (it already is if you used this repo's .gitignore).
""")
