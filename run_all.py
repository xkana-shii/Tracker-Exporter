#!/usr/bin/env python3
"""
run_all.py

Run selected tracker exporters concurrently by default (or sequentially with --sequential).
This version respects per-tracker enable flags in the .env in addition to credential checks.
"""

import os
import sys
import argparse
import subprocess
from config.config import should_run_tracker_with_reason

REPO_ROOT = os.path.abspath(os.path.dirname(__file__))

# Modules to run via python -m
TRACKERS = [
    ("mangaupdates", "mangaupdates.mu_main"),
    ("myanimelist", "myanimelist.mal_main"),
    ("mangabaka", "mangabaka.mb_main"),
]


def start_process(module_name: str, env: dict) -> subprocess.Popen:
    cmd = [sys.executable, "-m", module_name]
    print(f"Starting: {cmd} (cwd={REPO_ROOT})")
    return subprocess.Popen(cmd, env=env, cwd=REPO_ROOT)


def run_parallel(to_run: list[tuple[str, str]], env: dict) -> dict[str, int]:
    procs: dict[str, subprocess.Popen] = {}
    try:
        for name, module in to_run:
            procs[name] = start_process(module, env)

        exit_codes: dict[str, int] = {}
        for name, proc in procs.items():
            rc = proc.wait()
            exit_codes[name] = rc
            print(f"{name} exited with code {rc}")
        return exit_codes
    except KeyboardInterrupt:
        print("Interrupted — terminating children...")
        for p in procs.values():
            try:
                p.terminate()
            except Exception:
                pass
        for p in procs.values():
            try:
                p.wait(timeout=5)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        raise


def run_sequential(to_run: list[tuple[str, str]], env: dict) -> dict[str, int]:
    exit_codes: dict[str, int] = {}
    for name, module in to_run:
        print("=" * 60)
        print(f"Starting {name}")
        proc = start_process(module, env)
        try:
            rc = proc.wait()
        except KeyboardInterrupt:
            print("Interrupted — terminating child...")
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            raise
        exit_codes[name] = rc
        if rc != 0:
            print(f"{name} failed with exit code {rc}; stopping sequential run")
            break
        print(f"{name} completed successfully")
    return exit_codes


def main():
    parser = argparse.ArgumentParser(description="Run tracker exporters (concurrent by default).")
    parser.add_argument("--sequential", action="store_true", help="Run trackers one after another instead of concurrently")
    args = parser.parse_args()

    env = os.environ.copy()
    env["PYTHONPATH"] = REPO_ROOT + (os.pathsep + env.get("PYTHONPATH", "") if env.get("PYTHONPATH") else "")

    to_run = []
    skipped = {}
    for name, module in TRACKERS:
        ok, reason = should_run_tracker_with_reason(name)
        if ok:
            to_run.append((name, module))
        else:
            skipped[name] = reason or "missing credentials or disabled"

    if skipped:
        pairs = [f"{n} ({r})" for n, r in skipped.items()]
        print("Skipping trackers:", ", ".join(pairs))

    if not to_run:
        print("No trackers to run. Exiting.")
        return

    print("Will run:", ", ".join(n for n, _ in to_run))
    try:
        if args.sequential:
            exit_codes = run_sequential(to_run, env)
        else:
            exit_codes = run_parallel(to_run, env)
    except KeyboardInterrupt:
        print("Run interrupted by user")
        sys.exit(130)

    failed = {n: rc for n, rc in exit_codes.items() if rc != 0}
    if failed:
        print("Failures:", failed)
        sys.exit(1)
    else:
        print("All trackers completed successfully")
        sys.exit(0)


if __name__ == "__main__":
    main()
