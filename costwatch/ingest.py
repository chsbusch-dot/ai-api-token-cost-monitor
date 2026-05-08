"""CLI ingest for providers without an org-level cost API (Gemini, etc.).

Usage (from any script on the same host as costwatch):

    python -m costwatch.ingest <provider> <model> <input_tokens> <output_tokens> [source]

Examples:

    python -m costwatch.ingest gemini gemini-2.5-pro 1234 567 my-app
    python -m costwatch.ingest elevenlabs eleven-v3 0 12345 voice-script

For Gemini specifically, the values come from `response.usage_metadata`:
    prompt_token_count    → input_tokens
    candidates_token_count → output_tokens
"""
from __future__ import annotations

import sys

from .store import record_usage


def main() -> int:
    if len(sys.argv) < 5:
        print(
            "usage: python -m costwatch.ingest <provider> <model> "
            "<input_tokens> <output_tokens> [source]",
            file=sys.stderr,
        )
        return 1
    provider = sys.argv[1]
    model = sys.argv[2]
    try:
        in_tok = int(sys.argv[3])
        out_tok = int(sys.argv[4])
    except ValueError:
        print("error: input_tokens and output_tokens must be integers", file=sys.stderr)
        return 1
    source = sys.argv[5] if len(sys.argv) > 5 else None
    record_usage(provider, model, in_tok, out_tok, source)
    print(
        f"recorded {provider}/{model}: {in_tok} in / {out_tok} out "
        f"(source={source or '-'})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
