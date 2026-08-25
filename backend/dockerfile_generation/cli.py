"""dockerfile_generation CLI: dockagent-gen <repo_path> --doc README.md [--doc INSTALL.md]"""

from __future__ import annotations

import argparse
import os
import sys


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="dockagent-gen",
        description=(
            "Automatically generate a working Dockerfile for a project by running "
            "a build-error-repair loop driven by an LLM (DRAFT/ICSE 2026)."
        ),
    )
    parser.add_argument("repo_path", help="Path to the target repository.")
    parser.add_argument(
        "--doc",
        action="append",
        dest="docs",
        metavar="FILE",
        default=[],
        help="Path to a build doc (README, INSTALL, etc.) relative to repo_path or absolute. "
             "May be given up to twice.",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=6,
        metavar="N",
        help="Maximum build/repair iterations (default: 6).",
    )
    parser.add_argument(
        "--provider",
        default="gemini",
        choices=["gemini", "openai"],
        help="LLM provider (default: gemini).",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Model name. Defaults: gemini-flash-latest (Gemini), gpt-4o (OpenAI).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=900,
        metavar="SECONDS",
        help="Per-build timeout in seconds (default: 900).",
    )
    parser.add_argument(
        "--output",
        default="Dockerfile",
        metavar="FILENAME",
        help="Output filename written to repo_path on success (default: Dockerfile).",
    )
    parser.add_argument(
        "--optimize",
        action="store_true",
        help="After a successful build, rewrite as a multi-stage build to shrink the "
             "image (DRAFT Phase B). Falls back to the working Dockerfile if the "
             "rewrite cannot be made to build or is not smaller.",
    )

    args = parser.parse_args()

    repo_path = os.path.abspath(args.repo_path)
    if not os.path.isdir(repo_path):
        sys.exit(f"Error: repo_path '{repo_path}' is not a directory.")

    docs = args.docs[:2]
    if len(args.docs) > 2:
        print("Warning: only the first 2 docs will be used.", file=sys.stderr)

    doc_paths: list[str] = []
    for doc in docs:
        p = doc if os.path.isabs(doc) else os.path.join(repo_path, doc)
        doc_paths.append(p)

    from .build import RealDockerBuilder
    from .context import build_context
    from .generate import generate_initial
    from .llm import GeminiClient, OpenAIClient
    from .loop import run_loop

    print(f"[dockagent-gen] Scanning project and reading {len(doc_paths)} doc(s)...")
    context = build_context(doc_paths, repo_path)

    if args.provider == "gemini":
        model = args.model or "gemini-flash-latest"
        llm = GeminiClient(model=model)
    else:
        model = args.model or "gpt-4o"
        llm = OpenAIClient(model=model)
    builder = RealDockerBuilder(timeout=args.timeout)

    print("[dockagent-gen] Generating initial Dockerfile...")
    initial = generate_initial(context, llm)

    print(f"[dockagent-gen] Starting repair loop (max {args.max_attempts} attempts)...")
    result = run_loop(
        initial_dockerfile=initial,
        context=context,
        context_dir=repo_path,
        builder=builder,
        llm=llm,
        max_attempts=args.max_attempts,
    )

    if result.success:
        final_dockerfile = result.dockerfile

        if args.optimize:
            from .optimize import optimize

            print("[dockagent-gen] Phase B — optimizing image (multi-stage)…")
            opt = optimize(
                dockerfile=final_dockerfile,
                context=context,
                context_dir=repo_path,
                builder=builder,
                llm=llm,
                on_progress=lambda m: print(f"[dockagent-gen]   {m}"),
            )
            if opt.success:
                final_dockerfile = opt.dockerfile
                if opt.reduction_pct is not None:
                    print(
                        f"[dockagent-gen] Image size "
                        f"{opt.original_size / 1e6:.1f} MB → "
                        f"{opt.optimized_size / 1e6:.1f} MB "
                        f"({opt.reduction_pct:.1f}% smaller)."
                    )
                else:
                    print("[dockagent-gen] Optimization applied (size unknown).")
            else:
                print(f"[dockagent-gen] {opt.note}")

        output_path = os.path.join(repo_path, args.output)
        with open(output_path, "w", encoding="utf-8") as fh:
            fh.write(final_dockerfile)
        print(
            f"[dockagent-gen] Success after {result.attempts} attempt(s). "
            f"Dockerfile written to {output_path}"
        )
    else:
        print(
            f"[dockagent-gen] Failed after {result.attempts} attempt(s).",
            file=sys.stderr,
        )
        if result.last_error:
            print(f"[dockagent-gen] Last error: {result.last_error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
