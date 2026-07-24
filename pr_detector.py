"""
GitHub PR Detector
-------------------
Detects pull requests raised on a GitHub repo and pulls detailed info about
each one: files changed, lines added/removed, patch diffs, author, branch, etc.

Two modes of use:

1. ONE-SHOT / SPECIFIC PR
   python pr_detector.py --owner myorg --repo myrepo --pr 42

2. POLLING (keeps checking for new PRs on an interval, prints details as they show up)
   python pr_detector.py --owner myorg --repo myrepo --poll --interval 60

Auth:
   Put your token in a .env file in the same folder as this script:
       GITHUB_TOKEN=ghp_xxx...
   A token is optional for public repos but you'll be capped at 60 req/hr without one.
"""

import os
import sys
import time
import json
import argparse
from dataclasses import dataclass, field, asdict
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv()  # reads .env in the current working directory and populates os.environ

GITHUB_API = "https://api.github.com"


@dataclass
class FileChange:
    filename: str
    status: str          # "added", "modified", "removed", "renamed"
    additions: int
    deletions: int
    changes: int
    patch: Optional[str] = None  # unified diff text (may be None for binary/large files)


@dataclass
class PRDetails:
    number: int
    title: str
    author: str
    state: str
    created_at: str
    updated_at: str
    base_branch: str
    head_branch: str
    additions: int
    deletions: int
    changed_files: int
    commits: int
    url: str
    files: list = field(default_factory=list)  # list[FileChange]


class GitHubPRClient:
    def __init__(self, owner: str, repo: str, token: Optional[str] = None):
        self.owner = owner
        self.repo = repo
        self.session = requests.Session()
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        token = token or os.environ.get("GITHUB_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.session.headers.update(headers)

    def _get(self, path: str, params: dict = None):
        url = f"{GITHUB_API}{path}"
        resp = self.session.get(url, params=params or {})
        if resp.status_code == 403 and "rate limit" in resp.text.lower():
            reset = resp.headers.get("X-RateLimit-Reset")
            raise RuntimeError(f"Rate limited. Resets at epoch {reset}. Set GITHUB_TOKEN to raise the limit.")
        resp.raise_for_status()
        return resp

    def list_open_prs(self, per_page: int = 30):
        """Return raw PR summaries (open PRs), most recently updated first."""
        resp = self._get(
            f"/repos/{self.owner}/{self.repo}/pulls",
            params={"state": "open", "sort": "updated", "direction": "desc", "per_page": per_page},
        )
        return resp.json()

    def get_pr(self, pr_number: int) -> dict:
        resp = self._get(f"/repos/{self.owner}/{self.repo}/pulls/{pr_number}")
        return resp.json()

    def get_pr_files(self, pr_number: int, per_page: int = 100) -> list:
        """Handles pagination since a PR can touch more than 100 files."""
        files = []
        page = 1
        while True:
            resp = self._get(
                f"/repos/{self.owner}/{self.repo}/pulls/{pr_number}/files",
                params={"per_page": per_page, "page": page},
            )
            batch = resp.json()
            if not batch:
                break
            files.extend(batch)
            if len(batch) < per_page:
                break
            page += 1
        return files

    def post_pr_comment(self, pr_number: int, body: str):
        """
        Posts a comment on the PR conversation tab (the same endpoint used for
        issue comments — GitHub PRs are issues under the hood). This is how
        bots like CodeRabbit/Copilot show up with a summary comment.
        Requires a token with 'Pull requests: write' (or 'issues: write') permission.
        """
        resp = self.session.post(
            f"{GITHUB_API}/repos/{self.owner}/{self.repo}/issues/{pr_number}/comments",
            json={"body": body},
        )
        if resp.status_code == 403:
            raise RuntimeError(
                "403 Forbidden when posting comment — your token does not have write access "
                "to this repo. Fix: regenerate your token with 'Pull requests: Read and write' "
                f"permission (fine-grained) or the 'repo' scope (classic). GitHub said: {resp.text}"
            )
        resp.raise_for_status()
        return resp.json()

    def get_pr_details(self, pr_number: int) -> PRDetails:
        pr = self.get_pr(pr_number)
        raw_files = self.get_pr_files(pr_number)

        files = [
            FileChange(
                filename=f["filename"],
                status=f["status"],
                additions=f["additions"],
                deletions=f["deletions"],
                changes=f["changes"],
                patch=f.get("patch"),  # absent for binary files or very large diffs
            )
            for f in raw_files
        ]

        return PRDetails(
            number=pr["number"],
            title=pr["title"],
            author=pr["user"]["login"],
            state=pr["state"],
            created_at=pr["created_at"],
            updated_at=pr["updated_at"],
            base_branch=pr["base"]["ref"],
            head_branch=pr["head"]["ref"],
            additions=pr["additions"],
            deletions=pr["deletions"],
            changed_files=pr["changed_files"],
            commits=pr["commits"],
            url=pr["html_url"],
            files=files,
        )


def parse_patch_lines(patch: Optional[str]):
    """
    Splits a unified diff patch into clean added-line and removed-line lists,
    stripping the leading +/- markers and skipping the '+++'/'---' file headers
    and '@@' hunk markers. Returns (added_lines, removed_lines).
    """
    added, removed = [], []
    if not patch:
        return added, removed
    for line in patch.splitlines():
        if line.startswith("+++") or line.startswith("---") or line.startswith("@@"):
            continue
        if line.startswith("+"):
            added.append(line[1:])
        elif line.startswith("-"):
            removed.append(line[1:])
    return added, removed


def print_line_changes(details: PRDetails):
    """Prints a clean, per-file breakdown of exactly which lines were added vs removed."""
    print("=" * 70)
    print(f"PR #{details.number}: {details.title} — line-level changes")
    print("=" * 70)
    for f in details.files:
        added, removed = parse_patch_lines(f.patch)
        print(f"\n[{f.status}] {f.filename}")
        if not f.patch:
            print("  (no patch available — likely a binary file or diff too large to show)")
            continue
        if removed:
            print(f"  Removed ({len(removed)}):")
            for line in removed:
                print(f"    - {line}")
        if added:
            print(f"  Added ({len(added)}):")
            for line in added:
                print(f"    + {line}")
        if not added and not removed:
            print("  (no line-level changes detected, e.g. pure rename)")
    print("=" * 70)


def print_pr_summary(details: PRDetails, show_patch: bool = False):
    print("=" * 70)
    print(f"PR #{details.number}: {details.title}")
    print(f"Author: {details.author}   State: {details.state}")
    print(f"{details.base_branch} <- {details.head_branch}")
    print(f"Created: {details.created_at}   Updated: {details.updated_at}")
    print(f"Commits: {details.commits}   Files changed: {details.changed_files}")
    print(f"Lines: +{details.additions} / -{details.deletions}")
    print(f"URL: {details.url}")
    print("-" * 70)
    for f in details.files:
        print(f"  [{f.status:8}] {f.filename}  (+{f.additions} / -{f.deletions})")
        if show_patch and f.patch:
            print("    --- patch ---")
            for line in f.patch.splitlines():
                print(f"    {line}")
    print("=" * 70)


def poll_for_new_prs(client: GitHubPRClient, interval: int, show_patch: bool, post_comment: bool, show_lines: bool = False):
    """
    Repeatedly checks for open PRs. Tracks the 'updated_at' timestamp of the
    most recently seen PR per number so it only reports genuinely new activity
    (new PRs opened, or existing PRs updated with new commits).
    """
    seen_updated_at = {}  # pr_number -> last seen updated_at string
    print(f"Polling {client.owner}/{client.repo} every {interval}s for PR activity... (Ctrl+C to stop)")

    while True:
        try:
            prs = client.list_open_prs()
            for summary in prs:
                number = summary["number"]
                updated_at = summary["updated_at"]
                if seen_updated_at.get(number) != updated_at:
                    seen_updated_at[number] = updated_at
                    details = client.get_pr_details(number)
                    print_pr_summary(details, show_patch=show_patch)
                    if show_lines:
                        print_line_changes(details)
                    if post_comment:
                        try:
                            client.post_pr_comment(number, "Reviewed by DeployIQ")
                            print(f"  -> Posted comment on PR #{number}")
                        except (requests.exceptions.RequestException, RuntimeError) as e:
                            print(f"  -> Failed to post comment on PR #{number}: {e}", file=sys.stderr)
        except requests.exceptions.RequestException as e:
            # Covers connection drops, timeouts, DNS issues, HTTP errors, etc.
            # These are usually transient (flaky wifi, GitHub hiccup) so we log
            # and keep polling instead of crashing the whole script.
            print(f"Network/HTTP error while polling (will retry next cycle): {e}", file=sys.stderr)
        except RuntimeError as e:
            print(str(e), file=sys.stderr)

        time.sleep(interval)


def main():
    parser = argparse.ArgumentParser(description="Detect and inspect GitHub pull requests.")
    parser.add_argument("--owner", required=True, help="Repo owner/org, e.g. 'octocat'")
    parser.add_argument("--repo", required=True, help="Repo name, e.g. 'hello-world'")
    parser.add_argument("--pr", type=int, help="Specific PR number to inspect (one-shot mode)")
    parser.add_argument("--poll", action="store_true", help="Continuously poll for new/updated PRs")
    parser.add_argument("--interval", type=int, default=60, help="Polling interval in seconds (default 60)")
    parser.add_argument("--patch", action="store_true", help="Print full diff patches, not just summary lines")
    parser.add_argument("--comment", action="store_true", help="Post a 'Reviewed by DeployIQ' comment on the PR (requires a token with write access)")
    parser.add_argument("--lines", action="store_true", help="Print a clean per-file breakdown of exactly which lines were added vs removed")
    parser.add_argument("--json", action="store_true", help="Output raw JSON instead of formatted text (one-shot mode only)")
    parser.add_argument("--token", help="GitHub token (overrides GITHUB_TOKEN env var)")
    args = parser.parse_args()

    client = GitHubPRClient(args.owner, args.repo, token=args.token)

    if args.poll:
        poll_for_new_prs(client, interval=args.interval, show_patch=args.patch, post_comment=args.comment, show_lines=args.lines)
    elif args.pr:
        details = client.get_pr_details(args.pr)
        if args.json:
            print(json.dumps(asdict(details), indent=2))
        else:
            print_pr_summary(details, show_patch=args.patch)
            if args.lines:
                print_line_changes(details)
        if args.comment:
            client.post_pr_comment(args.pr, "Reviewed by DeployIQ")
            print(f"Posted comment on PR #{args.pr}")
    else:
        # No PR specified: show all currently open PRs
        prs = client.list_open_prs()
        if not prs:
            print("No open PRs found.")
            return
        for summary in prs:
            details = client.get_pr_details(summary["number"])
            print_pr_summary(details, show_patch=args.patch)
            if args.lines:
                print_line_changes(details)
            if args.comment:
                client.post_pr_comment(summary["number"], "Reviewed by DeployIQ")
                print(f"Posted comment on PR #{summary['number']}")


if __name__ == "__main__":
    main()