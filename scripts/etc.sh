#!/bin/bash

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ORIGINAL_BRANCH=$(git branch --show-current)

if [ -z "$1" ]; then
    echo "Usage: ./etc.sh <build_command>"
    echo "  e.g. ./etc.sh \"npx tsc --noEmit\""
    echo "  e.g. ./etc.sh \"npm run build\""
    exit 1
fi
BUILD_CMD="$1"

cleanup() {
    echo ""
    log_warning "Interrupted. Cleaning up..."
    git reset --hard HEAD 2>/dev/null
    git checkout "$ORIGINAL_BRANCH" 2>/dev/null
    git branch -D detangling 2>/dev/null
    git stash pop 2>/dev/null
    log_info "Restored to branch '$ORIGINAL_BRANCH' with original changes."
    exit 1
}
trap cleanup INT TERM

# Logging
log_info()    { echo -e "\033[1;34m[INFO]\033[0m $1"; }
log_error()   { echo -e "\033[1;31m[ERROR]\033[0m $1"; }
log_success() { echo -e "\033[1;32m[SUCCESS]\033[0m $1"; }
log_warning() { echo -e "\033[1;33m[WARNING]\033[0m $1"; }

# ----- Step 1: Create new detangling branch -----
git checkout -b detangling
log_info "Created and switched to 'detangling' branch."

# ----- Step 2: Save full patch -----
TEMP_PATCH=$(mktemp)
git add -N .          # include untracked files in diff
git diff -U0 > "$TEMP_PATCH"   # -U0: no context lines -> smallest possible hunks

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

# ----- Step 4: Create patch directories -----
HUNKS_DIR="patches/hunks"
mkdir -p "$HUNKS_DIR"
cp "$TEMP_PATCH" patches/full.patch
rm -f "$TEMP_PATCH"
log_info "Saved full patch to patches/full.patch"

# ----- Step 5: Split patch into individual hunks -----
log_info "Splitting full patch into individual hunks..."
awk '
    /^diff --git/ {
        diff_header = $0; file_minus = ""; file_plus = ""; current_file = ""; next
    }
    /^(old|new) mode|^(deleted|new) file mode/ {
        diff_header = diff_header "\n" $0; next
    }
    /^index / { diff_header = diff_header "\n" $0; next }
    /^---/    { file_minus = $0; next }
    /^\+\+\+/ { file_plus = $0; next }
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
    current_file != "" { print $0 >> current_file }
' "patches/full.patch"

HUNK_COUNT=$(find "$HUNKS_DIR" -maxdepth 1 -type f -name 'hunk_*.patch' | wc -l)
log_info "Split into $HUNK_COUNT individual hunks"

# ----- Step 6: Grouping and commit loop -----
#
# Each iteration:
#   1. Calls group.py with the current primary hunk + remaining pending hunks
#   2. group.py finds the smallest buildable group and leaves it applied
#   3. Commits what's applied, then remove those hunks from pending
#
log_info "Starting grouping (build: $BUILD_CMD)..."

PENDING=()
while IFS= read -r line; do
    PENDING+=("$line")
done < <(find "$HUNKS_DIR" -maxdepth 1 -type f -name 'hunk_*.patch' | sort)
COMMITTED_COUNT=0
SKIPPED=()

while [ ${#PENDING[@]} -gt 0 ]; do
    primary="${PENDING[0]}"
    rest=("${PENDING[@]:1}")

    log_info "── Processing $(basename "$primary") (${#PENDING[@]} hunks remaining) ──"

    # Call group.py to find the best group for this primary hunk
    GROUP=()
    while IFS= read -r line; do
        GROUP+=("$line")
    done < <(python3 "$SCRIPT_DIR/group.py" "$BUILD_CMD" "$primary" "${rest[@]}")

    if [ ${#GROUP[@]} -eq 0 ]; then
        log_warning "No buildable group found for $(basename "$primary") — skipping"
        SKIPPED+=("$primary")
        PENDING=("${rest[@]}")
        continue
    fi

    # Commit the group
    GROUP_NAMES=""
    for g in "${GROUP[@]}"; do
        [[ -n "$GROUP_NAMES" ]] && GROUP_NAMES+=", "
        GROUP_NAMES+=$(basename "$g")
    done
    log_success "Committing group: $GROUP_NAMES"

    git add -A
    git commit -m "etc: ${#GROUP[@]} hunk(s) — $GROUP_NAMES"
    (( COMMITTED_COUNT++ ))

    # Remove committed hunks from PENDING
    new_pending=()
    for h in "${PENDING[@]}"; do
        in_group=0
        for g in "${GROUP[@]}"; do
            [[ "$h" == "$g" ]] && in_group=1 && break
        done
        [[ $in_group -eq 0 ]] && new_pending+=("$h")
    done
    PENDING=("${new_pending[@]}")
done

# ----- Step 7: Results -----
log_success "Done: $COMMITTED_COUNT commit(s) created from $HUNK_COUNT hunks."

if [ ${#SKIPPED[@]} -gt 0 ]; then
    log_warning "${#SKIPPED[@]} hunk(s) could not be grouped and were skipped:"
    for h in "${SKIPPED[@]}"; do
        log_warning "  $(basename "$h")"
    done
fi

# ----- Step 8: Cleanup -----
#git checkout "$ORIGINAL_BRANCH"
#git branch -d "detangling"
#log_success "Returned to '$ORIGINAL_BRANCH' and deleted 'detangling' branch."