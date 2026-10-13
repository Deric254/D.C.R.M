"""Fails the build when the owner's public key is not set, so an installer can never go out without licence protection."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "backend"))
import auth  # noqa: E402

if auth.owner_public() is None:
    sys.exit("backend/owner_key.py has no valid OWNER_PUBLIC_KEY. Run python scripts/make-owner-key.py and paste the public key in. See README > Accounts and licences.")
print("Owner public key is set.")
