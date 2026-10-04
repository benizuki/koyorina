"""Export the k3s ServiceAccount issuer and public JWKS for PD CSI WIF."""

from __future__ import annotations

import base64
import json
import subprocess
import sys
from pathlib import Path


def kubectl(*args: str) -> str:
    return subprocess.check_output(["kubectl", *args], text=True)


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("usage: python export-pd-csi-oidc.py JWKS_OUTPUT_FILE")
    token = kubectl("-n", "default", "create", "token", "default", "--duration=10m").strip()
    payload = token.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    issuer = claims["iss"]
    jwks = json.loads(kubectl("get", "--raw", "/openid/v1/jwks"))
    if not issuer.startswith("https://") or not jwks.get("keys"):
        sys.exit("ServiceAccount issuer/JWKS is not valid for WIF")
    if len(jwks["keys"]) > 8:
        sys.exit("Google Cloud WIF accepts at most 8 uploaded JWKs")
    allowed = {"kty", "alg", "use", "kid", "n", "e", "x", "y", "crv"}
    jwks = {"keys": [{name: value for name, value in key.items() if name in allowed}
                     for key in jwks["keys"]]}
    target = Path(sys.argv[1]).expanduser().resolve()
    target.write_text(json.dumps(jwks, indent=2) + "\n")
    target.chmod(0o600)
    print(f"publication_pd_csi_issuer = {json.dumps(issuer)}")
    print(f"publication_pd_csi_jwks_file = {json.dumps(str(target))}")


if __name__ == "__main__":
    main()
