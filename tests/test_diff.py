"""Tests for diff extraction functionality."""

from autocommit.diff import (
    ChangeType,
    DiffHunk,
    GitDiffExtractor,
    extract_diffs,
)


def test_extract_diffs_integration():
    """Integration test: extract diffs from current repository."""
    # This test works with the actual repository state
    extractor = GitDiffExtractor(".")

    # Test that we can get changes (may be empty if no changes)
    staged = extractor.get_staged_changes()
    unstaged = extractor.get_unstaged_changes()
    all_changes = extractor.get_all_changes()

    # Basic assertions
    assert isinstance(staged, list)
    assert isinstance(unstaged, list)
    assert isinstance(all_changes, list)
    assert len(all_changes) == len(staged) + len(unstaged)

    # Each item should be a DiffHunk
    for hunk in all_changes:
        assert isinstance(hunk, DiffHunk)
        assert isinstance(hunk.change_type, ChangeType)
        assert hunk.file_path is not None


def test_get_untracked_files():
    """Test getting untracked files."""
    extractor = GitDiffExtractor(".")
    untracked = extractor.get_untracked_files()

    assert isinstance(untracked, list)
    # All items should be strings (file paths)
    for file_path in untracked:
        assert isinstance(file_path, str)


def test_diff_summary():
    """Test getting a summary of changes."""
    extractor = GitDiffExtractor(".")
    summary = extractor.get_diff_summary()

    # Check structure
    assert isinstance(summary, dict)
    assert "added" in summary
    assert "modified" in summary
    assert "deleted" in summary
    assert "renamed" in summary
    assert "untracked" in summary

    # All values should be non-negative integers
    for key, value in summary.items():
        assert isinstance(value, int)
        assert value >= 0


def test_change_type_enum():
    """Test ChangeType enum values."""
    assert ChangeType.ADDED.value == "A"
    assert ChangeType.MODIFIED.value == "M"
    assert ChangeType.DELETED.value == "D"
    assert ChangeType.RENAMED.value == "R"

    # Test that it's a string enum
    assert isinstance(ChangeType.ADDED, str)
    assert ChangeType.ADDED == "A"


def test_convenience_function():
    """Test the extract_diffs convenience function."""
    hunks = extract_diffs()

    assert isinstance(hunks, list)
    for hunk in hunks:
        assert isinstance(hunk, DiffHunk)


def test_has_changes():
    """Test has_changes method."""
    extractor = GitDiffExtractor(".")
    result = extractor.has_changes()

    # Should return a boolean
    assert isinstance(result, bool)


def test_diff_hunk_structure():
    """Test DiffHunk namedtuple structure if changes exist."""
    extractor = GitDiffExtractor(".")
    all_changes = extractor.get_all_changes()

    if all_changes:
        hunk = all_changes[0]

        # Check all required fields exist
        assert hasattr(hunk, 'file_path')
        assert hasattr(hunk, 'old_content')
        assert hasattr(hunk, 'new_content')
        assert hasattr(hunk, 'a_path')
        assert hasattr(hunk, 'b_path')
        assert hasattr(hunk, 'change_type')
        assert hasattr(hunk, 'diff_text')

        # Check types
        assert isinstance(hunk.file_path, str)
        assert isinstance(hunk.old_content, str)
        assert isinstance(hunk.new_content, str)
        assert isinstance(hunk.change_type, ChangeType)
        assert isinstance(hunk.diff_text, str)


if __name__ == "__main__":
    print("Running diff extraction tests...\n")

    extractor = GitDiffExtractor(".")

    # Show current changes
    print("=== Staged Changes ===")
    staged = extractor.get_staged_changes()
    if staged:
        for hunk in staged:
            print(f"  {hunk.change_type.name:10} {hunk.file_path}")
    else:
        print("  (none)")

    print("\n=== Unstaged Changes ===")
    unstaged = extractor.get_unstaged_changes()
    if unstaged:
        for hunk in unstaged:
            print(f"  {hunk.change_type.name:10} {hunk.file_path}")
            print(f"    Lines: {len(hunk.old_content.splitlines())} -> {len(hunk.new_content.splitlines())}")
    else:
        print("  (none)")

    print("\n=== Untracked Files ===")
    untracked = extractor.get_untracked_files()
    if untracked:
        for file in untracked:
            print(f"  {file}")
    else:
        print("  (none)")

    print("\n=== Summary ===")
    summary = extractor.get_diff_summary()
    for key, value in summary.items():
        print(f"  {key}: {value}")

    print("\n✓ Manual test completed successfully")
