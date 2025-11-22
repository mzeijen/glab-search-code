#!/usr/bin/env python3
"""
GitLab Code Search & Download Tool

Usage:
    glab-search-code <search_query> --hostname HOST [--workers N]

Examples:
    glab-search-code 'GeneratedValue' --hostname gitlab.example.com
    glab-search-code 'class MyService' --hostname gitlab.example.com --workers 20
"""

import argparse
import asyncio
import base64
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import yaml


class Colors:
    RED = "\033[0;31m"
    GREEN = "\033[0;32m"
    YELLOW = "\033[1;33m"
    BLUE = "\033[0;34m"
    NC = "\033[0m"


def get_glab_hostnames() -> list[str]:
    # Parse glab config to get available GitLab hostnames. This ensures users
    # can only specify hostnames they've already configured, preventing typos
    # and misconfiguration.
    config_path = Path.home() / ".config" / "glab-cli" / "config.yml"

    if not config_path.exists():
        return []

    try:
        with open(config_path) as f:
            config = yaml.safe_load(f)

        if not config or "hosts" not in config:
            return []

        return list(config["hosts"].keys())
    except (yaml.YAMLError, OSError):
        return []


class GitLabSearcher:
    def __init__(
        self,
        search_term: str,
        hostname: str | None = None,
        workers: int = 10,
        max_retries: int = 3,
        retry_delay: int = 2,
    ):
        # Timestamp in directory name prevents accidental overwrites when running
        # multiple searches, and sanitizing ensures filesystem compatibility.
        self.search_term = search_term
        self.hostname = hostname
        self.workers = workers
        self.max_retries = max_retries
        self.retry_delay = retry_delay

        sanitized = re.sub(r"[^a-zA-Z0-9._-]", "_", search_term).lower()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.output_dir = Path(f"/tmp/gitlab-search-{sanitized}-{timestamp}")
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.log_file = self.output_dir / "download.log"
        self.metadata_file = self.output_dir / "metadata.json"

        # Track stats separately to provide detailed failure reporting, which is
        # critical when dealing with rate limits and transient API errors.
        self.successful = 0
        self.skipped = 0
        self.failed = 0

        # Cache avoids redundant API calls since many files often belong to the
        # same project. Pre-fetching all projects upfront is faster than fetching
        # on-demand during downloads.
        self.project_cache: dict[str, str] = {}

        self.log(f"Script started - search query: {search_term}")

    def log(self, message: str):
        # Single-line atomic appends ensure log integrity even when parallel workers
        # write simultaneously. Timestamps help correlate failures with retries.
        timestamp = datetime.now().isoformat()
        with open(self.log_file, "a") as f:
            f.write(f"[{timestamp}] {message}\n")

    def print_color(self, message: str, color: str = ""):
        # Color coding helps quickly identify success/failures in terminal output
        # without having to parse text. NC (No Color) reset prevents color bleed.
        if color:
            print(f"{color}{message}{Colors.NC}")
        else:
            print(message)

    async def run_glab(self, *args) -> tuple[str, str, int]:
        # Async subprocess allows parallel GitLab API calls without blocking threads.
        # Returning all three values (stdout, stderr, code) enables proper error handling
        # and retry logic based on specific failure types (e.g., 429 rate limits).
        # Hostname flag is injected here to ensure all glab calls use the correct instance.
        glab_args = ["glab"]
        if self.hostname:
            glab_args.extend(["--hostname", self.hostname])
        glab_args.extend(args)

        proc = await asyncio.create_subprocess_exec(
            *glab_args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        return stdout.decode(), stderr.decode(), proc.returncode

    async def search_all(self) -> list[dict]:
        # Using --paginate is essential for large result sets (>100 files) as it
        # automatically handles pagination instead of requiring manual page tracking.
        self.print_color("\nFetching all search results (this may take a moment)...", Colors.BLUE)
        self.log(f"Starting search for: {self.search_term}")

        stdout, stderr, code = await self.run_glab(
            "api", f"search?scope=blobs&search={self.search_term}&per_page=100", "--paginate"
        )

        if code != 0:
            self.log(f"ERROR: Search failed - {stderr}")
            self.print_color("Search failed. Check log file.", Colors.RED)
            sys.exit(1)

        # glab --paginate returns concatenated JSON arrays (][][]) instead of a
        # single array, so we must fix the format before parsing. This quirk is
        # specific to glab's pagination implementation.
        try:
            fixed_json = stdout.replace("][", ",")
            results = json.loads(fixed_json)
        except json.JSONDecodeError as e:
            self.log(f"ERROR: Failed to parse search results - {e}")
            self.print_color("Failed to parse search results", Colors.RED)
            sys.exit(1)

        self.log(f"Search completed: {len(results)} results")
        self.print_color(f"{Colors.GREEN}Found {len(results)} results{Colors.NC}")

        return results

    async def get_project_path(self, project_id: str) -> str:
        # Caching is critical because multiple files often share the same project,
        # and fetching project info adds significant API overhead.
        if project_id in self.project_cache:
            return self.project_cache[project_id]

        stdout, stderr, code = await self.run_glab("api", f"projects/{project_id}")

        if code == 0:
            try:
                project_data = json.loads(stdout)
                path = project_data.get("path_with_namespace", f"project-{project_id}")
                self.project_cache[project_id] = path
                return path
            except json.JSONDecodeError:
                pass

        # Fallback to ID-based name ensures the script continues even if project
        # metadata fetch fails, preventing total failure due to transient API issues.
        self.project_cache[project_id] = f"project-{project_id}"
        return self.project_cache[project_id]

    async def prefetch_projects(self, results: list[dict]):
        # Pre-fetching all projects in parallel before downloads avoids repeated
        # cache misses during downloads. This front-loaded cost significantly speeds
        # up the overall process compared to lazy on-demand fetching.
        self.print_color("\nPre-fetching project information...", Colors.BLUE)
        self.log("Starting project cache pre-fetch")

        unique_projects = {str(r["project_id"]) for r in results}

        tasks = [self.get_project_path(pid) for pid in unique_projects]
        await asyncio.gather(*tasks)

        self.log(f"Project cache completed: {len(unique_projects)} projects")
        self.print_color(f"{Colors.GREEN}Cached {len(unique_projects)} projects{Colors.NC}\n")

    def sanitize_filename(self, project_path: str, file_path: str) -> str:
        # Combining project path with file path prevents collisions when different
        # projects have files with the same name. Double underscores for path
        # separators are visually distinct and filesystem-safe.
        combined = f"{project_path}__{file_path}"
        return re.sub(r"[^a-zA-Z0-9._-]", "_", combined.replace("/", "__"))

    async def download_file(self, item: dict, semaphore: asyncio.Semaphore) -> dict:
        # Semaphore limits concurrent downloads to prevent overwhelming the GitLab API
        # and triggering rate limits. Workers default to 10 as a balance between speed
        # and staying under rate limit thresholds.
        async with semaphore:
            project_id = str(item["project_id"])
            file_path = item["filename"]
            ref = item["ref"]

            project_path = await self.get_project_path(project_id)
            sanitized = self.sanitize_filename(project_path, file_path)
            output_file = self.output_dir / sanitized

            # Skipping existing files allows resuming interrupted downloads and avoids
            # wasted bandwidth on re-running the same search query.
            if output_file.exists():
                self.log(f"SKIP: {project_path}/{file_path} (file already exists)")
                return {"status": "SKIP", "project_path": project_path, "file_path": file_path}

            # Retry loop specifically handles HTTP 429 (rate limit) errors with
            # exponential backoff, which is the most common failure mode with parallel
            # downloads. Non-rate-limit errors fail fast without retries.
            for retry in range(self.max_retries):
                # URL encoding is required because file paths can contain special characters
                # like spaces, which break the GitLab API if not properly encoded.
                encoded_path = quote(file_path, safe="")
                stdout, stderr, code = await self.run_glab(
                    "api", f"projects/{project_id}/repository/files/{encoded_path}?ref={ref}"
                )

                if code == 0:
                    try:
                        # GitLab returns file content base64-encoded, requiring decode
                        # before writing to disk.
                        file_data = json.loads(stdout)
                        content = base64.b64decode(file_data["content"])
                        output_file.write_bytes(content)

                        if retry > 0:
                            self.log(f"OK: {project_path}/{file_path} (after {retry} retries)")
                        else:
                            self.log(f"OK: {project_path}/{file_path}")

                        return {
                            "status": "OK",
                            "project_id": project_id,
                            "project_path": project_path,
                            "file_path": file_path,
                            "ref": ref,
                            "output_file": sanitized,
                        }
                    except (json.JSONDecodeError, KeyError) as e:
                        self.log(f"FAIL: {project_path}/{file_path} - Error: Parse error {e}")
                        return {"status": "FAIL", "project_path": project_path, "file_path": file_path}

                # Only retry on 429 errors (rate limiting). Other errors (403, 404, etc.)
                # are permanent and should not be retried.
                if "429" in stderr and retry < self.max_retries - 1:
                    # Exponential backoff reduces API load and increases success rate
                    # on subsequent retries after rate limit window expires.
                    sleep_time = self.retry_delay * (retry + 1)
                    self.log(
                        f"RETRY: {project_path}/{file_path} - Rate limited, retry {retry + 1}/{self.max_retries} after {sleep_time}s"
                    )
                    await asyncio.sleep(sleep_time)
                    continue

                error_msg = stderr.strip().split("\n")[0] if stderr else "unknown error"
                if retry >= self.max_retries - 1:
                    self.log(
                        f"FAIL: {project_path}/{file_path} - Error: {error_msg} (gave up after {self.max_retries} retries)"
                    )
                else:
                    self.log(f"FAIL: {project_path}/{file_path} - Error: {error_msg}")
                return {"status": "FAIL", "project_path": project_path, "file_path": file_path}

            return {"status": "FAIL", "project_path": project_path, "file_path": file_path}

    def print_progress(self, total: int):
        # Real-time progress bar provides feedback during long downloads and helps
        # identify if the script has stalled. Using \r to overwrite the same line
        # keeps output compact instead of spamming hundreds of lines.
        completed = self.successful + self.skipped + self.failed
        percent = (completed / total * 100) if total > 0 else 0
        bar_length = 40
        filled = int(bar_length * completed / total) if total > 0 else 0
        bar = "=" * filled + "-" * (bar_length - filled)

        print(
            f"\r[{bar}] {completed}/{total} ({percent:.1f}%) | "
            f"{Colors.GREEN}✓{self.successful}{Colors.NC} "
            f"{Colors.BLUE}⊘{self.skipped}{Colors.NC} "
            f"{Colors.RED}✗{self.failed}{Colors.NC}",
            end="",
            flush=True,
        )

    async def download_all(self, results: list[dict]):
        # asyncio.as_completed() yields results as they finish, allowing immediate
        # progress updates instead of waiting for all downloads to complete. This
        # provides better user feedback during long-running downloads.
        self.print_color(f"Downloading files with {self.workers} parallel workers...", Colors.BLUE)
        self.log(f"Starting parallel downloads with {self.workers} workers")

        semaphore = asyncio.Semaphore(self.workers)
        tasks = [self.download_file(item, semaphore) for item in results]
        total = len(tasks)

        metadata = []
        for coro in asyncio.as_completed(tasks):
            result = await coro

            if result["status"] == "OK":
                self.successful += 1
                # Exclude 'status' from metadata since it's temporary tracking data,
                # not useful for the metadata file which is used to locate downloaded files.
                metadata.append({k: v for k, v in result.items() if k != "status"})
            elif result["status"] == "SKIP":
                self.skipped += 1
            else:
                self.failed += 1

            self.print_progress(total)

        print()

        # Metadata file maps sanitized filenames back to original GitLab locations,
        # enabling users to find where a downloaded file came from.
        self.print_color("\nFinalizing metadata...", Colors.BLUE)
        with open(self.metadata_file, "w") as f:
            json.dump(metadata, f, indent=2)

        self.log(f"Downloads completed - OK: {self.successful}, SKIP: {self.skipped}, FAIL: {self.failed}")
        self.log("Script completed successfully")

    async def run(self):
        # Main orchestration method that coordinates the three phases: search, cache,
        # download. Timing each phase helps identify bottlenecks (e.g., slow search vs
        # rate-limited downloads).
        self.print_color("========================================", Colors.BLUE)
        self.print_color("GitLab Code Search & Download", Colors.BLUE)
        self.print_color("========================================", Colors.BLUE)
        if self.hostname:
            self.print_color(f"GitLab hostname: {Colors.YELLOW}{self.hostname}{Colors.NC}")
        self.print_color(f"Search query: {Colors.YELLOW}{self.search_term}{Colors.NC}")
        self.print_color(f"Output directory: {Colors.YELLOW}{self.output_dir}{Colors.NC}")
        self.print_color(f"Parallel jobs: {Colors.YELLOW}{self.workers}{Colors.NC}")

        import time

        start = time.time()
        results = await self.search_all()
        search_duration = int(time.time() - start)
        self.print_color(f"Search completed in {Colors.GREEN}{search_duration}s{Colors.NC}\n")

        if not results:
            self.print_color("No results found", Colors.RED)
            return

        # Pre-fetching is separated as its own timed phase because it significantly
        # impacts total runtime and helps diagnose if GitLab API is slow.
        start = time.time()
        await self.prefetch_projects(results)
        prefetch_duration = int(time.time() - start)
        self.print_color(f"Project pre-fetch completed in {Colors.GREEN}{prefetch_duration}s{Colors.NC}\n")

        start = time.time()
        await self.download_all(results)
        download_duration = int(time.time() - start)

        # Summary
        self.print_color("\n========================================", Colors.GREEN)
        self.print_color("Download complete!", Colors.GREEN)
        self.print_color("========================================", Colors.GREEN)
        self.print_color(f"Search duration: {Colors.GREEN}{search_duration}s{Colors.NC}")
        self.print_color(f"Project pre-fetch duration: {Colors.GREEN}{prefetch_duration}s{Colors.NC}")
        self.print_color(f"Download duration: {Colors.GREEN}{download_duration}s{Colors.NC}")
        self.print_color(f"Total files downloaded: {Colors.GREEN}{self.successful}{Colors.NC}")
        self.print_color(f"Files skipped (existed): {Colors.BLUE}{self.skipped}{Colors.NC}")
        self.print_color(f"Failed downloads: {Colors.RED}{self.failed}{Colors.NC}")
        self.print_color(f"Output directory: {Colors.YELLOW}{self.output_dir}{Colors.NC}")
        self.print_color(f"Metadata file: {Colors.YELLOW}{self.metadata_file}{Colors.NC}")
        self.print_color(f"Log file: {Colors.YELLOW}{self.log_file}{Colors.NC}")

        print("\n" + Colors.BLUE + "Useful commands:" + Colors.NC)
        print(Colors.BLUE + "────────────────────────────────────────" + Colors.NC)
        print("View download log (including all failures):")
        print(f"  {Colors.YELLOW}less {self.log_file}{Colors.NC}")
        print("\nView only failed downloads:")
        print(f"  {Colors.YELLOW}grep FAIL {self.log_file}{Colors.NC}")
        print("\nSearch downloaded files:")
        print(f"  {Colors.YELLOW}grep -r 'pattern' {self.output_dir}{Colors.NC}")
        print("\nSee unique projects:")
        print(f"  {Colors.YELLOW}jq -r '.[] | .project_path' {self.metadata_file} | sort -u{Colors.NC}")
        print()


async def main():
    # Get available hostnames early to include in help text and error messages.
    # This makes the tool more user-friendly by showing what's actually configured.
    available_hosts = get_glab_hostnames()

    if available_hosts:
        hostname_help = f"GitLab hostname to search. Available: {', '.join(available_hosts)}"
    else:
        hostname_help = "GitLab hostname to search (must be configured in glab)"

    parser = argparse.ArgumentParser(
        description="GitLab Code Search & Download Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s 'GeneratedValue' --hostname gitlab.example.com
  %(prog)s 'class MyService' --hostname gitlab.example.com --workers 20
        """,
    )
    parser.add_argument("search_query", help="Search term to find in GitLab")
    parser.add_argument("--hostname", required=True, help=hostname_help)
    parser.add_argument("--workers", type=int, default=10, help="Number of parallel downloads (default: 10)")
    parser.add_argument("--max-retries", type=int, default=3, help="Max retries for rate-limited requests (default: 3)")

    # Custom error handling to show available hostnames when --hostname is missing
    try:
        args = parser.parse_args()
    except SystemExit as e:
        if e.code == 2 and available_hosts and "--hostname" not in sys.argv:  # Argument parsing error
            # Check if --hostname was the issue by looking at sys.argv
            print(f"\n{Colors.YELLOW}Available hostnames:{Colors.NC}")
            for host in available_hosts:
                print(f"  - {host}")
        raise

    # Validate hostname - must match a configured glab host to prevent typos and
    # ensure authentication is set up.
    if not available_hosts:
        print(f"{Colors.RED}Error: Could not read glab config file{Colors.NC}")
        print(f"Expected location: {Path.home() / '.config' / 'glab-cli' / 'config.yml'}")
        sys.exit(1)

    if args.hostname not in available_hosts:
        print(f"{Colors.RED}Error: Hostname '{args.hostname}' is not configured in glab{Colors.NC}")
        print(f"\n{Colors.YELLOW}Available hostnames:{Colors.NC}")
        for host in available_hosts:
            print(f"  - {host}")
        print(f"\nConfigure a new hostname with: {Colors.BLUE}glab auth login --hostname {args.hostname}{Colors.NC}")
        sys.exit(1)

    if args.workers < 1 or args.workers > 50:
        print(f"{Colors.RED}Error: workers must be between 1 and 50{Colors.NC}")
        sys.exit(1)

    searcher = GitLabSearcher(
        args.search_query, hostname=args.hostname, workers=args.workers, max_retries=args.max_retries
    )
    await searcher.run()


def cli():
    """Entry point for the command line interface."""
    asyncio.run(main())


if __name__ == "__main__":
    cli()
