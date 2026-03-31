#!/bin/bash

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ORIGINAL_BRANCH=$(git branch --show-current)

if [ -z "$1" ]; then
    echo "Usage: ./etc.sh <build_command>"
    echo "  e.g. ./etc.sh \"dotnet build src/CommandLine/CommandLine.csproj --no-restore\""
    echo "  e.g. ./etc.sh \"npx tsc --noEmit\""
    exit 1
fi
BUILD_CMD="$1"

# ----- Metrics log setup -----
# Structured event log consumed by metrics.py after the run.
# Each line is: TIMESTAMP|EVENT|DATA
METRICS_LOG="patches/metrics.log"

metrics_event() {
    local event="$1"
    local data="$2"
    echo "$(date +%s%3N)|${event}|${data}" >> "$METRICS_LOG"
}

cleanup() {
    echo ""
    log_warning "Interrupted. Cleaning up..."
    metrics_event "INTERRUPTED" ""
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

# ------------------------------------------------------------------ #
#  split_hunks <patch_file> <hunks_dir>                              #
#  Splits a unified diff into one file per hunk under <hunks_dir>.  #
#  Clears <hunks_dir> first so stale files never linger.            #
# ------------------------------------------------------------------ #
split_hunks() {
    local patch_file="$1"
    local hunks_dir="$2"
    rm -f "$hunks_dir"/hunk_*.patch
    awk '
        /^diff --git/ {
            diff_header = $0; file_minus = ""; file_plus = ""; current_file = ""; next
        }
        /^(old|new) mode|^(deleted|new) file mode/ {
            diff_header = diff_header "\n" $0; next
        }
        /^index / { diff_header = diff_header "\n" $0; next }
        /^---/    { file_minus = $0; next }
        /^\+\+\+/ { file_plus  = $0; next }
        /^@@/ {
            hunk_count++
            filename = sprintf("'"$hunks_dir"'/hunk_%04d.patch", hunk_count)
            print diff_header > filename
            print file_minus  > filename
            print file_plus   > filename
            print $0          > filename
            current_file = filename
            next
        }
        current_file != "" { print $0 >> current_file }
    ' "$patch_file"
}

# ----- Step 1: Create new detangling branch -----
git checkout -b detangling
log_info "Created and switched to 'detangling' branch."

# ----- Step 2: Check there are changes to process -----
git add -N .   # surface untracked files so they appear in diff
TOTAL_LINES=$(git diff -U0 | wc -l)
if [ "$TOTAL_LINES" -eq 0 ]; then
    log_warning "No changes found."
    git checkout "$ORIGINAL_BRANCH"
    git branch -d detangling
    exit 0
fi

# ----- Step 3: Save the full original patch for reference -----
mkdir -p patches
git diff -U0 > patches/full.patch
log_info "Full patch saved to patches/full.patch"

# ----- Step 4: Stash all changes — working tree is now clean -----
log_info "Stashing original working directory changes..."
git stash --include-untracked
log_success "Changes stashed. Working tree is clean."

# ----- Step 5: Initialise metrics log and patch dirs -----
HUNKS_DIR="patches/hunks"
mkdir -p "$HUNKS_DIR"
echo "" > "$METRICS_LOG"   # overwrite any previous run

# Count total hunks from the original full patch for reference
TOTAL_HUNK_COUNT=$(grep -c "^@@" patches/full.patch || echo 0)
log_info "Total hunks in full patch: $TOTAL_HUNK_COUNT"
metrics_event "RUN_START" "total_hunks=${TOTAL_HUNK_COUNT},build_cmd=${BUILD_CMD}"

# ----- Step 6: Grouping and commit loop -----
#
# Every iteration:
#   1. Pop the stash into the working tree
#   2. Re-diff against current HEAD -> fresh, correctly-offset hunks
#   3. Re-stash
#   4. Re-split into hunk files
#   5. If no hunks remain, we are done
#   6. Pass all current hunks to group.py -> ddmin finds minimal buildable subset
#   7. group.py leaves the winning group applied; commit it
#   8. Loop (next iteration re-diffs against the newly updated HEAD)
#
log_info "Starting grouping and commit loop (build: $BUILD_CMD)..."

COMMITTED_COUNT=0
ITERATION=0

while true; do
    (( ITERATION++ ))
    ITER_START=$(date +%s%3N)

    # --- 6.1  Pop stash and regenerate diff relative to current HEAD ---
    git stash pop
    git add -N .
    git diff -U0 > "patches/remaining_iter_${ITERATION}.patch"

    REMAINING_LINES=$(wc -l < "patches/remaining_iter_${ITERATION}.patch")
    if [ "$REMAINING_LINES" -eq 0 ]; then
        # Diff is empty — all changes have been committed
        log_success "All changes have been committed."
        rm -f "patches/remaining_iter_${ITERATION}.patch"
        break
    fi

    HUNK_COUNT=$(grep -c "^@@" "patches/remaining_iter_${ITERATION}.patch" || echo 0)
    log_info "── Iteration ${ITERATION}: ${HUNK_COUNT} hunk(s) remaining ──"
    metrics_event "ITER_START" "iteration=${ITERATION},pending=${HUNK_COUNT}"

    # Re-stash so the working tree is clean for group.py to apply/revert patches
    git stash --include-untracked

    # --- 6.2  Split the fresh remaining patch into individual hunk files ---
    split_hunks "patches/remaining_iter_${ITERATION}.patch" "$HUNKS_DIR"

    PENDING=()
    while IFS= read -r f; do
        PENDING+=("$f")
    done < <(find "$HUNKS_DIR" -maxdepth 1 -type f -name 'hunk_*.patch' | sort)

    # --- 6.3  Call group.py with all current hunks ---
    GROUP_OUTPUT=$(python3 "$SCRIPT_DIR/scripts/group.py" "$BUILD_CMD" "${PENDING[@]}" \
        2>"patches/iter_${ITERATION}_invocations.log")
    GROUP_EXIT=$?

    INVOCATIONS=$(grep -c "probe" "patches/iter_${ITERATION}_invocations.log" 2>/dev/null || echo 0)
    cat "patches/iter_${ITERATION}_invocations.log" >&2   # echo probes to terminal

    GROUP=()
    if [ $GROUP_EXIT -eq 0 ] && [ -n "$GROUP_OUTPUT" ]; then
        while IFS= read -r line; do
            [[ -n "$line" ]] && GROUP+=("$line")
        done <<< "$GROUP_OUTPUT"
    fi

    ITER_END=$(date +%s%3N)
    ITER_DURATION=$(( ITER_END - ITER_START ))

    # --- 6.4  Handle failure: ddmin found no buildable subset ---
    if [ ${#GROUP[@]} -eq 0 ]; then
        log_warning "No buildable group found for the remaining ${HUNK_COUNT} hunk(s)."
        log_warning "Leaving remaining changes in the stash — inspect manually."
        metrics_event "ITER_FAILED" \
            "iteration=${ITERATION},invocations=${INVOCATIONS},duration_ms=${ITER_DURATION},remaining=${HUNK_COUNT}"
        break
    fi

    # --- 6.5  Commit the winning group (group.py already left it applied) ---
    GROUP_NAMES=""
    for g in "${GROUP[@]}"; do
        [[ -n "$GROUP_NAMES" ]] && GROUP_NAMES+=", "
        GROUP_NAMES+=$(basename "$g")
    done
    log_success "Committing group (${#GROUP[@]} hunk(s)): $GROUP_NAMES"

    git add -A
    git commit -m "etc[${ITERATION}]: ${#GROUP[@]} hunk(s) — $GROUP_NAMES"
    (( COMMITTED_COUNT++ ))

    metrics_event "ITER_COMMIT" \
        "iteration=${ITERATION},group_size=${#GROUP[@]},invocations=${INVOCATIONS},duration_ms=${ITER_DURATION},hunks=${GROUP_NAMES}"

    # Loop: next iteration pops the stash and re-diffs against the new HEAD
done

# ----- Step 7: Results -----
metrics_event "RUN_END" "commits=${COMMITTED_COUNT}"

log_success "Done: $COMMITTED_COUNT commit(s) produced from $TOTAL_HUNK_COUNT hunk(s)."
log_info "Full original patch : patches/full.patch"
log_info "Per-iteration diffs : patches/remaining_iter_*.patch"
log_info "Per-iteration probes: patches/iter_*_invocations.log"
log_info "Metrics log         : patches/metrics.log"
log_info "Run:  python3 scripts/metrics.py patches/metrics.log  for a summary."

# ----- Step 8: Cleanup (uncomment when happy with results) -----
#git checkout "$ORIGINAL_BRANCH"
#git branch -d detangling
#log_success "Returned to '$ORIGINAL_BRANCH' and deleted 'detangling' branch."