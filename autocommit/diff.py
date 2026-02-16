"""Extract and parse diffs from a git working directory."""

from enum import Enum
from pathlib import Path
from typing import NamedTuple

from git import Repo
from git.diff import Diff


class ChangeType(str, Enum):
    """Types of changes in a git diff."""

    ADDED = "A"
    MODIFIED = "M"
    DELETED = "D"
    RENAMED = "R"


class DiffHunk(NamedTuple):
    """Hunk of changes in a file."""

    file_path: str
    old_content: str
    new_content: str
    a_path: str | None  # Path in the old version (None for new files)
    b_path: str | None  # Path in the new version (None for deleted files)
    change_type: ChangeType
    diff_text: str  # The actual unified diff text


class GitDiffExtractor:
    """Extract diffs from a git repository working directory."""

    def __init__(self, repo_path: str | Path = "."):
        """Initialize the diff extractor with a repository path.

        Args:
            repo_path: Path to the git repository (defaults to current directory)
        """
        
        self.repo = Repo(repo_path, search_parent_directories=True)

    def get_unstaged_changes(self) -> list[DiffHunk]:
        """Extract all unstaged changes in the working directory.

        Returns:
            List of DiffHunk objects representing unstaged changes
        """
        
        diffs = self.repo.index.diff(None, create_patch=True)
        return self._parse_diffs(diffs)

    def get_staged_changes(self) -> list[DiffHunk]:
        """Extract all staged changes.

        Returns:
            List of DiffHunk objects representing staged changes
        """
        
        diffs = self.repo.index.diff("HEAD", create_patch=True)
        return self._parse_diffs(diffs)

    def get_all_changes(self) -> list[DiffHunk]:
        """Extract both staged and unstaged changes.

        Returns:
            List of DiffHunk objects representing all changes
        """
        
        return self.get_staged_changes() + self.get_unstaged_changes()

    def get_untracked_files(self) -> list[str]:
        """Get list of untracked files in the repository.

        Returns:
            List of file paths that are not tracked by git
        """
        
        return self.repo.untracked_files

    def _parse_diffs(self, diffs: list[Diff]) -> list[DiffHunk]:
        """Parse git Diff objects into DiffHunk objects.

        Args:
            diffs: List of git.diff.Diff objects from GitPython

        Returns:
            List of parsed DiffHunk objects
        """
        
        hunks = []

        for diff in diffs:
            # Find change type
            change_type = self._get_change_type(diff)

            # Get file paths
            a_path = diff.a_path if diff.a_path else None
            b_path = diff.b_path if diff.b_path else None
            file_path = b_path if b_path else a_path

            # Get content
            old_content = ""
            new_content = ""

            if diff.a_blob:
                try:
                    old_content = diff.a_blob.data_stream.read().decode("utf-8")
                except (UnicodeDecodeError, AttributeError):
                    # Skip binary files or files that can't be decoded
                    continue

            if diff.b_blob:
                try:
                    new_content = diff.b_blob.data_stream.read().decode("utf-8")
                except (UnicodeDecodeError, AttributeError):
                    # Skip binary files or files that can't be decoded
                    continue

            # Get the unified diff text
            diff_text = ""
            if hasattr(diff, "diff") and diff.diff:
                try:
                    diff_text = diff.diff.decode("utf-8")
                except (UnicodeDecodeError, AttributeError):
                    diff_text = ""

            hunk = DiffHunk(
                file_path=file_path,
                old_content=old_content,
                new_content=new_content,
                a_path=a_path,
                b_path=b_path,
                change_type=change_type,
                diff_text=diff_text,
            )
            hunks.append(hunk)

        return hunks

    def _get_change_type(self, diff: Diff) -> ChangeType:
        """Determine the type of change from a Diff object.

        Args:
            diff: A git.diff.Diff object

        Returns:
            ChangeType enum representing the type of change
        """
        
        if diff.new_file:
            return ChangeType.ADDED
        elif diff.deleted_file:
            return ChangeType.DELETED
        elif diff.renamed_file:
            return ChangeType.RENAMED
        else:
            return ChangeType.MODIFIED

    def has_changes(self) -> bool:
        """Check if there are any changes in the working directory.

        Returns:
            True if there are staged, unstaged, or untracked changes
        """
        
        return (
            len(self.get_staged_changes()) > 0
            or len(self.get_unstaged_changes()) > 0
            or len(self.get_untracked_files()) > 0
        )

    def get_diff_summary(self) -> dict[str, int]:
        """Get a summary of changes.

        Returns:
            Dictionary with counts of added, modified, deleted, and renamed files
        """
        
        all_changes = self.get_all_changes()

        summary = {"added": 0, "modified": 0, "deleted": 0, "renamed": 0, "untracked": 0}

        for hunk in all_changes:
            if hunk.change_type == ChangeType.ADDED:
                summary["added"] += 1
            elif hunk.change_type == ChangeType.MODIFIED:
                summary["modified"] += 1
            elif hunk.change_type == ChangeType.DELETED:
                summary["deleted"] += 1
            elif hunk.change_type == ChangeType.RENAMED:
                summary["renamed"] += 1

        summary["untracked"] = len(self.get_untracked_files())

        return summary


def extract_diffs(repo_path: str | Path = ".") -> list[DiffHunk]:
    """Extracts all changes from a repository.

    Args:
        repo_path: Path to the git repository (defaults to current directory)

    Returns:
        List of DiffHunk objects representing all changes
    """
    
    extractor = GitDiffExtractor(repo_path)
    return extractor.get_all_changes()
