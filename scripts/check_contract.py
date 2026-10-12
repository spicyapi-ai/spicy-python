"""Contract drift check: every contract fact hard-coded in this package must still match the
published contract.

Why this check has to exist: the enums, error codes and limits in an SDK are all constants copied
from the contract. When the contract changes and these do not, nothing raises an error - requests
still go out, responses still parse, it is only that some new option gets rejected locally as an
illegal value, or some new error code is flattened into "unknown error". This kind of failure emits
no signal.

Why it compares against the published contract rather than the spicy-server repository: this
repository is public and spicy-server is not. Giving a public repository's CI a token that can read
a private one puts that token somewhere anyone can open a pull request against. The contract is
public anyway, so comparing public against public needs no credentials at all.

``--offline`` skips the download and checks the constants against the pinned copy alone. It is for
syncing the pin from the server's contract before that contract is published: it proves the
package and the pin agree, and says nothing about whether the pin is current. CI runs the default
mode.
"""

from __future__ import annotations

import pathlib
import re
import sys
import urllib.request

LIVE_URL = "https://docs.spicyapi.ai/openapi.yaml"
ROOT = pathlib.Path(__file__).parents[1]
LOCAL = ROOT / "contracts" / "openapi.yaml"


def enum_after(text: str, anchor: str, field: str, window_chars: int = 4000) -> set[str]:
    """Return the enum values of ``field`` in the section following ``anchor``."""
    # A section or field that has gone missing is drift too, and deserves a sentence naming it
    # rather than a bare "substring not found" traceback.
    start = text.find(anchor)
    if start < 0:
        raise SystemExit(f"[contract] section {anchor.strip()!r} is missing from the contract")
    window = text[start : start + window_chars]
    field_at = window.find(field)
    if field_at < 0:
        raise SystemExit(f"[contract] no {field.strip()!r} found after {anchor.strip()!r}")
    match = re.search(r"enum:\s*\[([^\]]+)\]", window[field_at : field_at + 600])
    if not match:
        raise SystemExit(f"no enum found for {field!r} after {anchor!r}")
    return {value.strip().strip("'\"") for value in match.group(1).split(",")}


def main(argv: list[str]) -> int:
    offline = "--offline" in argv
    if offline:
        live = LOCAL.read_text(encoding="utf-8")
    else:
        # A User-Agent is mandatory: the docs site's edge protection answers 403 to urllib's
        # default UA (Python-urllib/3.x), while the api host does not. Both were tested on
        # 2026-09-20 and they behave differently.
        request = urllib.request.Request(
            LIVE_URL, headers={"User-Agent": "spicyapi-contract-check"}
        )
        live = urllib.request.urlopen(request, timeout=30).read().decode("utf-8")

        if not LOCAL.exists() or LOCAL.read_text(encoding="utf-8") != live:
            print(
                "[contract] contracts/openapi.yaml is stale against the published contract.\n"
                f"[contract] refresh it:  curl -s {LIVE_URL} -o contracts/openapi.yaml\n"
                "[contract] then re-check every constant this package hard-codes against that "
                "diff.",
                file=sys.stderr,
            )
            return 1

    sys.path.insert(0, str(ROOT / "src"))
    from spicyapi import _client

    # Only the handful of facts whose drift actually causes damage. A short list gets maintained;
    # a long one accumulates entries nobody reads.
    checks = [
        (
            "task states",
            _client.TERMINAL_STATES | _client.ACTIVE_STATES,
            enum_after(live, "    TaskRecord:", "state:"),
        ),
        (
            "upload content types",
            set(_client.UPLOAD_CONTENT_TYPES),
            enum_after(live, "    UploadURLRequest:", "contentType:"),
        ),
        # The asset library's failure reasons: a reason the package does not know still reaches
        # the caller verbatim, but is_asset_error_reason would call it unknown.
        (
            "asset error reasons",
            set(_client.ASSET_ERROR_REASONS),
            enum_after(live, "    ErrorEnvelope:", "                reason:", 8000),
        ),
        # Sorts are refused locally, so a sort the server gains would be refused by the package
        # while the server plainly accepts it.
        (
            "asset list sorts",
            set(_client.ASSET_SORTS),
            enum_after(live, "operationId: listAssets", "name: sort", 8000),
        ),
        (
            "asset sources",
            set(_client.ASSET_SOURCES),
            enum_after(live, "    AssetCreateRequest:", "source:"),
        ),
    ]

    failed = False
    for name, ours, theirs in checks:
        if ours != theirs:
            print(
                f"[contract] {name} drifted:\n"
                f"  package:  {sorted(ours)}\n"
                f"  contract: {sorted(theirs)}",
                file=sys.stderr,
            )
            failed = True

    if failed:
        return 1
    against = "the pinned contract (offline)" if offline else "the published contract"
    print(f"[contract] matches {against} ({len(checks)} constant sets verified).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
