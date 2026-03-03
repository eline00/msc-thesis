#!/bin/bash

ORIGINAL_BRANCH=$(git branch --show-current)

# Logging
log_info() { echo -e "\033[1;34m[INFO]\033[0m $1"; }
log_error() { echo -e "\033[1;31m[ERROR]\033[0m $1"; }
log_success() { echo -e "\033[1;32m[SUCCESS]\033[0m $1"; }
log_warning() { echo -e "\033[1;33m[WARNING]\033[0m $1"; }

# ----- Step 1: Create new detangling branch -----
git checkout -b detangling
log_info "Created and switched to 'detangling' branch."

# ----- Step 2: Save full patch to temp file -----
TEMP_PATCH=$(mktemp)
git diff > "$TEMP_PATCH"

TOTAL_LINES=$(wc -l < "$TEMP_PATCH")
if [ "$TOTAL_LINES" -eq 0 ]; then
    log_warning "No changes found"
    rm -f "$TEMP_PATCH"
    git checkout "$ORIGINAL_BRANCH"
    git branch -d "detangling"
    exit 0
fi
log_info "Full patch saved ($TOTAL_LINES lines)"

# ----- Step 3: Stash original changes -----
log_info "Stashing original working directory changes..."
git stash --include-untracked
log_success "Original changes stashed (working directory is now clean)"

# ----- Step 4: Create patching directories and save patches -----
HUNKS_DIR="patches/hunks"
mkdir -p "$HUNKS_DIR"
log_info "Created directory for hunks: $HUNKS_DIR"

cp "$TEMP_PATCH" patches/full.patch
rm -f "$TEMP_PATCH"
log_info "Saved full patch to patches/full.patch"

# ----- Step 5: Split patch into hunks -----
# Each hunk gets its own patch with the necessary diff headers
log_info "Splitting full patch into individual hunks..."
awk '
    /^diff --git/ {
        diff_header = $0
        file_minus = ""
        file_plus = ""
        current_file = ""
        next
    }
    /^(old|new) mode/ {
        diff_header = diff_header "\n" $0
        next
    }
    /^(deleted|new) file mode/ {
        diff_header = diff_header "\n" $0
        next
    }
    /^index / {
        diff_header = diff_header "\n" $0
        next
    }
    /^---/ {
        file_minus = $0
        next
    }
    /^\+\+\+/ {
        file_plus = $0
        next
    }
    /^@@/ {
        hunk_count++
        filename = sprintf("'"$HUNKS_DIR"'/hunk_%04d.patch", hunk_count)
        print diff_header > filename
        print file_minus > filename
        print file_plus > filename
        print $0 > filename
        current_file = filename
        next
    }
    current_file != "" {
        print $0 >> current_file
    }
' "patches/full.patch"

HUNK_COUNT=$(find "$HUNKS_DIR" -maxdepth 1 -type f -name 'hunk_*.patch' | wc -l)
log_info "Split into $HUNK_COUNT individual hunks"

# ----- Step 6: Apply hunks one by one -----
log_info "Applying hunks one by one..."
for hunk in "$HUNKS_DIR"/hunk_*.patch; do
    # Check if hunk can be applied cleanly
    log_info "Applying $(basename "$hunk")..."
    if git apply --check "$hunk" 2>/dev/null; then
        # Apply the hunk
        git apply "$hunk"
        log_success "Applied $(basename "$hunk") successfully"

        # Run build
        log_info "Running build after applying $(basename "$hunk")..."
        if git diff --name-only HEAD -- '*.py' | xargs -r python -m py_compile; then
            log_success "Build passed after applying $(basename "$hunk")"

            # Commit the hunk
            git add -A
            git commit -m "Applied hunk: $(basename "$hunk")"
        else
            log_error "Build failed after applying $(basename "$hunk")"
            log_warning "Reverting $(basename "$hunk") and moving to next hunk"
            git apply -R "$hunk"
        fi
    else
        log_error "Failed to apply $(basename "$hunk")"
        log_warning "Skipping $(basename "$hunk") and moving to next hunk"
    fi
done

#TODO: push to remote and create PR?

# ----- Step 7: Cleanup -----
log_info "All hunks processed. Finalizing..."
git checkout "$ORIGINAL_BRANCH"
git branch -d "detangling"
log_success "Returned to original branch '$ORIGINAL_BRANCH' and deleted 'detangling' branch."
log_info "Detangling process completed."