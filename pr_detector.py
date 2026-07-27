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

Feature extraction (--features):
   Computes 12 Just-In-Time defect-prediction style features (ns, nd, nf, ent,
   la, ld, ndev, age, nuc, aexp, arexp, asexp) for each PR using local Git
   history via GitPython. See GitFeatureExtractor below for details on how
   each feature is computed. The GitHub API does not expose full repo history,
   so the repo is cloned locally once (and fetched on subsequent runs) to
   /local_repos/<owner>_<repo> by default.
"""

import os
import sys
import time
import json
import math
import argparse
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from typing import Optional, List

import requests
from dotenv import load_dotenv

import git
from git.exc import GitCommandError, InvalidGitRepositoryError, NoSuchPathError

from google import genai
from google.genai import types as genai_types

load_dotenv()  # reads .env in the current working directory and populates os.environ

GITHUB_API = "https://api.github.com"

# How many days count as "recent" for the arexp (recent author experience) feature.
# Kept as a module-level constant so it's easy to tune without touching the logic.
RECENT_EXPERIENCE_DAYS = 90

# Latest stable Gemini model, used for post-FeatureVector deployment-risk analysis.
GEMINI_MODEL = "gemini-3.6-flash"

SYSTEM_PROMPT = """You are a deployment-risk analyst for software pull requests.

You will be given a PR's metadata, its changed files, its added/removed lines,
and a 12-feature Just-In-Time (JIT) defect-prediction feature vector (ns, nd,
nf, ent, la, ld, ndev, age, nuc, aexp, arexp, asexp).

Analyze the deployment risk of this PR and respond with ONLY a single valid
JSON object matching exactly this schema, with no markdown, no code fences,
and no text before or after it:

{
  "risk_score": <integer 0-100>,
  "risk_level": "<Low|Medium|High|Critical>",
  "summary": "<2-3 sentence explanation of the risk assessment>",
  "top_risk_factors": ["<factor 1>", "<factor 2>", "<factor 3>"],
  "recommendations": ["<recommendation 1>", "<recommendation 2>", "<recommendation 3>"]
}"""


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


@dataclass
class FeatureVector:
    """
    The 12 Just-In-Time (JIT) defect-prediction style features computed for
    a single pull request, ready to be fed into an ML model.
    """
    ns: int        # Number of subsystems changed
    nd: int        # Number of directories changed
    nf: int        # Number of files changed
    ent: float     # Change entropy
    la: int        # Lines added
    ld: int        # Lines deleted
    ndev: int      # Number of previous developers who modified the changed files
    age: float     # Average age (in days) of the changed files
    nuc: float     # Average number of previous commits touching the changed files
    aexp: int      # Author experience (total historical commits by the author)
    arexp: int     # Recent author experience (commits in the last N days)
    asexp: int     # Author subsystem experience (author commits within touched subsystems)

    def to_dict(self) -> dict:
        """Returns the feature vector as a plain dict, ready for ML inference."""
        return asdict(self)


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


class LocalRepoManager:
    """
    Handles getting a local, full-history clone of the repo so GitPython can
    walk commit history for feature extraction (the GitHub REST API alone
    does not expose full repo history the way a local clone does).

    - Clones the repo automatically on first use.
    - On subsequent runs, reuses the existing clone and just fetches updates.
    - Never re-clones on every execution.
    """

    def __init__(self, owner: str, repo: str, local_path: Optional[str] = None, token: Optional[str] = None):
        self.owner = owner
        self.repo_name = repo
        self.token = token or os.environ.get("GITHUB_TOKEN")
        self.local_path = local_path or os.path.join(os.getcwd(), "local_repos", f"{owner}_{repo}")

    def _clone_url(self) -> str:
        # Embed the token in the HTTPS URL so private repos can be cloned too.
        # Public repos work the same way without a token.
        if self.token:
            return f"https://{self.token}@github.com/{self.owner}/{self.repo_name}.git"
        return f"https://github.com/{self.owner}/{self.repo_name}.git"

    def get_repo(self) -> git.Repo:
        """Returns a ready-to-use git.Repo, cloning or fetching as needed."""
        git_dir = os.path.join(self.local_path, ".git")

        if os.path.isdir(self.local_path) and os.path.isdir(git_dir):
            try:
                repo = git.Repo(self.local_path)
            except (InvalidGitRepositoryError, NoSuchPathError) as e:
                raise RuntimeError(f"'{self.local_path}' exists but is not a valid git repo: {e}")
            try:
                # Pull down any new commits since the last run so history-based
                # features (ndev, age, nuc, aexp, arexp, asexp) stay accurate.
                repo.remotes.origin.fetch(prune=True)
            except GitCommandError as e:
                print(f"Warning: fetch failed, using existing local history ({e})", file=sys.stderr)
            return repo

        os.makedirs(os.path.dirname(self.local_path) or ".", exist_ok=True)
        print(f"Cloning {self.owner}/{self.repo_name} into '{self.local_path}' (one-time setup)...")
        try:
            repo = git.Repo.clone_from(self._clone_url(), self.local_path)
        except GitCommandError as e:
            raise RuntimeError(
                f"Failed to clone {self.owner}/{self.repo_name}. Check the repo name/owner and that "
                f"your token (if private) has read access. Git said: {e}"
            )
        return repo


class GitFeatureExtractor:
    """
    Computes the 12 JIT defect-prediction features for a PR using local git
    history via GitPython. All computations are done against a reference
    branch (the PR's base branch by default, falling back to HEAD) so results
    reflect the state of the codebase the PR is being merged into.

    Note on author matching: GitHub PR data gives us the author's GitHub
    *login*, but git commit history only has an author *name*/*email* (the
    git identity configured on their machine, which is often different from
    their GitHub username). We do a best-effort case-insensitive substring
    match against both the commit author's name and email. If your team's
    GitHub logins don't resemble their git identities, pass a mapping in
    via --git-author-name for one-off testing, or extend
    GitFeatureExtractor._author_matches with a real login->identity table.
    """

    def __init__(self, repo: git.Repo, recent_experience_days: int = RECENT_EXPERIENCE_DAYS):
        self.repo = repo
        self.recent_experience_days = recent_experience_days

    # ---------- helpers ----------

    @staticmethod
    def _subsystem(filename: str) -> str:
        """Top-level folder of a path, e.g. 'backend/auth/login.java' -> 'backend'."""
        parts = filename.split("/")
        return parts[0] if len(parts) > 1 else "(root)"

    @staticmethod
    def _directory(filename: str) -> str:
        d = os.path.dirname(filename)
        return d if d else "(root)"

    def _resolve_rev(self, preferred: str) -> str:
        """
        Falls back to HEAD if the preferred ref (e.g. 'origin/main') isn't
        resolvable locally -- covers edge cases like a base branch that was
        renamed/deleted or a shallow/partial clone.
        """
        try:
            self.repo.commit(preferred)
            return preferred
        except (GitCommandError, ValueError, git.BadName):
            return "HEAD"

    def _file_commits(self, filename: str, rev: str):
        """All commits touching a given file path, newest first."""
        try:
            return list(self.repo.iter_commits(rev=rev, paths=filename))
        except GitCommandError:
            return []

    @staticmethod
    def _author_matches(commit, author_identity: str) -> bool:
        needle = author_identity.lower()
        name = (commit.author.name or "").lower()
        email = (commit.author.email or "").lower()
        return needle in name or needle in email

    def _author_commits(self, author_identity: str, rev: str, since: Optional[datetime] = None,
                         paths: Optional[List[str]] = None):
        kwargs = {}
        if since is not None:
            kwargs["since"] = since.strftime("%Y-%m-%d")
        try:
            commits = self.repo.iter_commits(rev=rev, paths=paths, **kwargs)
        except GitCommandError:
            return []
        return [c for c in commits if self._author_matches(c, author_identity)]

    # ---------- individual feature computations ----------

    def compute_ns(self, files: List[FileChange]) -> int:
        """Number of unique top-level subsystems (folders) touched."""
        return len({self._subsystem(f.filename) for f in files})

    def compute_nd(self, files: List[FileChange]) -> int:
        """Number of unique directories touched."""
        return len({self._directory(f.filename) for f in files})

    def compute_ent(self, files: List[FileChange]) -> float:
        """
        Shannon entropy of the change distribution across files:
        -sum(pi * log2(pi)), where pi = file_changes / total_changes.
        Files with zero changes are skipped (log2(0) is undefined).
        """
        total = sum(f.changes for f in files)
        if total == 0:
            return 0.0
        entropy = 0.0
        for f in files:
            if f.changes == 0:
                continue
            pi = f.changes / total
            entropy -= pi * math.log2(pi)
        return entropy

    def compute_ndev(self, files: List[FileChange], rev: str) -> int:
        """Unique developers who have ever touched any of the changed files."""
        authors = set()
        for f in files:
            for commit in self._file_commits(f.filename, rev):
                authors.add(commit.author.email or commit.author.name or "unknown")
        return len(authors)

    def compute_age(self, files: List[FileChange], reference_date: datetime, rev: str) -> float:
        """
        Average age (in days) of the changed files, where a file's age is
        (reference_date - date of its first/oldest commit).
        """
        ages_days = []
        for f in files:
            commits = self._file_commits(f.filename, rev)
            if not commits:
                continue
            oldest_commit = commits[-1]  # iter_commits yields newest -> oldest
            first_date = datetime.fromtimestamp(oldest_commit.committed_date, tz=timezone.utc)
            ages_days.append((reference_date - first_date).days)
        return sum(ages_days) / len(ages_days) if ages_days else 0.0

    def compute_nuc(self, files: List[FileChange], rev: str) -> float:
        """Average number of prior commits touching each changed file."""
        counts = [len(self._file_commits(f.filename, rev)) for f in files]
        return sum(counts) / len(counts) if counts else 0.0

    def compute_aexp(self, author_identity: str, rev: str) -> int:
        """Total historical commit count by the PR author across the whole repo."""
        return len(self._author_commits(author_identity, rev=rev))

    def compute_arexp(self, author_identity: str, reference_date: datetime, rev: str) -> int:
        """Commits by the PR author within the last `recent_experience_days` days."""
        since = reference_date - timedelta(days=self.recent_experience_days)
        return len(self._author_commits(author_identity, rev=rev, since=since))

    def compute_asexp(self, author_identity: str, files: List[FileChange], rev: str) -> int:
        """Commits by the PR author within the subsystems the PR touches."""
        subsystems = sorted({self._subsystem(f.filename) for f in files if self._subsystem(f.filename) != "(root)"})
        if not subsystems:
            return 0
        return len(self._author_commits(author_identity, rev=rev, paths=subsystems))

    # ---------- entry point ----------

    def extract(self, details: PRDetails, reference_date: Optional[datetime] = None) -> FeatureVector:
        """
        Computes the full 12-feature vector for a PR. History-based features
        are computed against the PR's base branch (falling back to HEAD if
        that ref can't be resolved locally, e.g. a partial/shallow clone).
        """
        reference_date = reference_date or datetime.now(timezone.utc)
        rev = self._resolve_rev(f"origin/{details.base_branch}")
        files = details.files

        return FeatureVector(
            ns=self.compute_ns(files),
            nd=self.compute_nd(files),
            nf=details.changed_files,
            ent=round(self.compute_ent(files), 4),
            la=details.additions,
            ld=details.deletions,
            ndev=self.compute_ndev(files, rev=rev),
            age=round(self.compute_age(files, reference_date, rev=rev), 2),
            nuc=round(self.compute_nuc(files, rev=rev), 2),
            aexp=self.compute_aexp(details.author, rev=rev),
            arexp=self.compute_arexp(details.author, reference_date, rev=rev),
            asexp=self.compute_asexp(details.author, files, rev=rev),
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


class GeminiRiskAnalyzer:
    """
    Sends a PR's metadata, files, added/removed lines, and FeatureVector to
    Gemini using a fixed system prompt, and returns the model's structured
    risk assessment as a Python dict.
    """

    def __init__(self, api_key: Optional[str] = None, model: str = GEMINI_MODEL):
        api_key = api_key or os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY not set (add it to your .env file)")
        self.client = genai.Client(api_key=api_key)
        self.model = model

    @staticmethod
    def _build_user_message(details: PRDetails, fv: FeatureVector) -> str:
        added_lines, removed_lines = [], []
        for f in details.files:
            a, r = parse_patch_lines(f.patch)
            added_lines.extend(a)
            removed_lines.extend(r)

        files_block = "\n".join(f"- {f.filename} ({f.status})" for f in details.files) or "(none)"
        added_block = "\n".join(added_lines) or "(none)"
        removed_block = "\n".join(removed_lines) or "(none)"

        return (
            "PR\n\n"
            f"Title:\n{details.title}\n\n"
            f"Author:\n{details.author}\n\n"
            f"Files:\n{files_block}\n\n"
            f"Added lines:\n{added_block}\n\n"
            f"Removed lines:\n{removed_block}\n\n"
            "Feature Vector\n\n"
            f"{json.dumps(fv.to_dict(), indent=2)}"
        )

    def analyze(self, details: PRDetails, fv: FeatureVector) -> dict:
        """Sends the PR + FeatureVector to Gemini and returns the parsed JSON response."""
        user_message = self._build_user_message(details, fv)
        response = self.client.models.generate_content(
            model=self.model,
            contents=user_message,
            config=genai_types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                response_mime_type="application/json",
            ),
        )
        raw_text = response.text
        try:
            return json.loads(raw_text)
        except json.JSONDecodeError:
            return {"raw_response": raw_text}


def save_output(pr_number: int, repository: str, fv: FeatureVector, llm_response: dict) -> str:
    """Saves the FeatureVector + Gemini risk assessment to outputs/pr_<PR_NUMBER>.json."""
    os.makedirs("outputs", exist_ok=True)
    payload = {
        "pr_number": pr_number,
        "repository": repository,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "feature_vector": fv.to_dict(),
        "llm_response": llm_response,
    }
    path = os.path.join("outputs", f"pr_{pr_number}.json")
    with open(path, "w") as fp:
        json.dump(payload, fp, indent=2)
    return path


def _run_risk_analysis(analyzer: "GeminiRiskAnalyzer", details: PRDetails, fv: FeatureVector, repository: str):
    """Calls GeminiRiskAnalyzer, saves the result, and prints the summary block. Never raises."""
    try:
        risk = analyzer.analyze(details, fv)
    except Exception:
        print("LLM analysis failed")
        return
    output_path = save_output(details.number, repository, fv, risk)
    print("-" * 50)
    print("Deployment Risk")
    print()
    print(f"Risk Score : {risk.get('risk_score', 'N/A')}")
    print(f"Risk Level : {str(risk.get('risk_level', 'N/A')).upper()}")
    print()
    print("Summary :")
    print(risk.get("summary", ""))
    print()
    print("Saved to")
    print(output_path)
    print("-" * 50)


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


def print_feature_vector(fv: FeatureVector, pr_number: int):
    """Prints the 12-feature vector in the simple 'key: value' format for quick reading."""
    print("-" * 70)
    print(f"Feature Vector (PR #{pr_number})")
    print(f"ns: {fv.ns}")
    print(f"nd: {fv.nd}")
    print(f"nf: {fv.nf}")
    print(f"ent: {fv.ent}")
    print(f"la: {fv.la}")
    print(f"ld: {fv.ld}")
    print(f"ndev: {fv.ndev}")
    print(f"age: {fv.age}")
    print(f"nuc: {fv.nuc}")
    print(f"aexp: {fv.aexp}")
    print(f"arexp: {fv.arexp}")
    print(f"asexp: {fv.asexp}")
    print("-" * 70)


def poll_for_new_prs(client: GitHubPRClient, interval: int, show_patch: bool, post_comment: bool,
                     show_lines: bool = False, feature_extractor: Optional[GitFeatureExtractor] = None,
                     analyzer: Optional["GeminiRiskAnalyzer"] = None):
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
                    if feature_extractor is not None:
                        try:
                            # Re-fetch so history-based features reflect the latest commits.
                            feature_extractor.repo.remotes.origin.fetch(prune=True)
                        except GitCommandError as e:
                            print(f"  -> Warning: fetch before feature extraction failed: {e}", file=sys.stderr)
                        fv = feature_extractor.extract(details)
                        print_feature_vector(fv, number)
                        if analyzer is not None:
                            _run_risk_analysis(analyzer, details, fv, f"{client.owner}/{client.repo}")
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
    parser.add_argument("--features", action="store_true",
                        help="Compute and print the 12 ML-ready features (ns, nd, nf, ent, la, ld, ndev, age, nuc, aexp, arexp, asexp)")
    parser.add_argument("--repo-path", help="Local path to clone/reuse for history analysis (default: ./local_repos/<owner>_<repo>)")
    parser.add_argument("--recent-days", type=int, default=RECENT_EXPERIENCE_DAYS,
                        help=f"Window size in days for the arexp feature (default {RECENT_EXPERIENCE_DAYS})")
    args = parser.parse_args()

    client = GitHubPRClient(args.owner, args.repo, token=args.token)

    feature_extractor = None
    analyzer = None
    if args.features:
        repo_manager = LocalRepoManager(args.owner, args.repo, local_path=args.repo_path, token=args.token)
        local_repo = repo_manager.get_repo()
        feature_extractor = GitFeatureExtractor(local_repo, recent_experience_days=args.recent_days)
        try:
            analyzer = GeminiRiskAnalyzer()
        except RuntimeError as e:
            print(f"Warning: Gemini risk analysis disabled ({e})", file=sys.stderr)

    if args.poll:
        poll_for_new_prs(client, interval=args.interval, show_patch=args.patch, post_comment=args.comment,
                         show_lines=args.lines, feature_extractor=feature_extractor, analyzer=analyzer)
    elif args.pr:
        details = client.get_pr_details(args.pr)
        if args.json:
            print(json.dumps(asdict(details), indent=2))
        else:
            print_pr_summary(details, show_patch=args.patch)
            if args.lines:
                print_line_changes(details)
        if feature_extractor is not None:
            fv = feature_extractor.extract(details)
            if args.json:
                print(json.dumps(fv.to_dict(), indent=2))
            else:
                print_feature_vector(fv, args.pr)
            if analyzer is not None:
                _run_risk_analysis(analyzer, details, fv, f"{args.owner}/{args.repo}")
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
            if feature_extractor is not None:
                fv = feature_extractor.extract(details)
                print_feature_vector(fv, summary["number"])
                if analyzer is not None:
                    _run_risk_analysis(analyzer, details, fv, f"{args.owner}/{args.repo}")
            if args.comment:
                client.post_pr_comment(summary["number"], "Reviewed by DeployIQ")
                print(f"Posted comment on PR #{summary['number']}")


if __name__ == "__main__":
    main()