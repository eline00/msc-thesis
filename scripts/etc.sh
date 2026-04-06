#!/bin/bash

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ORIGINAL_BRANCH=$(git branch --show-current)

BUILD_CMD="dotnet build --no-restore"

# ----- Metrics log setup -----
# Structured event log consumed by metrics.py after the run.
# Each line is: TIMESTAMP|EVENT|DATA
METRICS_LOG="patches/metrics.log"

metrics_event() {
    local event="$1"
    local data="$2"
    echo "$(python3 -c "import time; print(int(time.time()*1000))")|${event}|${data}" >> "$METRICS_LOG"
}

cleanup() {
    echo ""
    log_warning "Interrupted. Cleaning up..."
    metrics_event "INTERRUPTED" ""
    git reset --hard "$TANGLED_SHA" 2>/dev/null
    git checkout "$ORIGINAL_BRANCH" 2>/dev/null
    git branch -D detangling 2>/dev/null
    log_info "Restored to branch '$ORIGINAL_BRANCH'."
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

# ----- Step 4: Temporarily commit all changes as the tangled state, then reset back -----
log_info "Creating temporary tangled commit..."
git add -A
git commit -m "tangled changes"
TANGLED_SHA=$(git rev-parse HEAD) # store SHA for later diffing
log_info "Tangled SHA: $TANGLED_SHA"

# Reset back to the clean state (before tangled commit)
git reset --hard HEAD~1
log_success "Reset to clean state. Working tree is clean."

# ----- Step 5: Initialise metrics log and patch dirs -----
HUNKS_DIR="patches/hunks"
GROUPS_DIR="patches/groups"
mkdir -p "$HUNKS_DIR" "$GROUPS_DIR"
echo "" > "$METRICS_LOG"

TOTAL_HUNK_COUNT=$(grep -c "^@@" patches/full.patch || echo 0)
log_info "Total hunks in full patch: $TOTAL_HUNK_COUNT"
metrics_event "RUN_START" "total_hunks=${TOTAL_HUNK_COUNT},build_cmd=${BUILD_CMD}"

# ----- Step 6: Grouping loop -----
log_info "Starting grouping loop..."

GROUP_COUNT=0
ITERATION=0

while true; do
    (( ITERATION++ ))
    ITER_START=$(python3 -c "import time; print(int(time.time()*1000))")

    # Re-diff HEAD against tangled SHA
    REMAINING_PATCH="patches/remaining_iter_${ITERATION}.patch"
    git diff -U0 HEAD "$TANGLED_SHA" > "$REMAINING_PATCH"

    REMAINING_HUNK_COUNT=$(grep -c "^@@" "$REMAINING_PATCH" 2>/dev/null || echo 0)
    if [ "$REMAINING_HUNK_COUNT" -eq 0 ]; then
        log_success "All changes have been grouped."
        rm -f "$REMAINING_PATCH"
        break
    fi

    log_info "── Iteration ${ITERATION}: ${REMAINING_HUNK_COUNT} hunk(s) remaining ──"
    metrics_event "ITER_START" "iteration=${ITERATION},pending=${REMAINING_HUNK_COUNT}"

    # Split new diff into individual hunk files
    split_hunks "$REMAINING_PATCH" "$HUNKS_DIR"

    PENDING=()
    while IFS= read -r f; do
        PENDING+=("$f")
    done < <(find "$HUNKS_DIR" -maxdepth 1 -type f -name 'hunk_*.patch' | sort)

    # Call group.py with all current hunks
    GROUP_OUTPUT=$(python3 "$SCRIPT_DIR/group.py" "$BUILD_CMD" "${PENDING[@]}" \
        2>"patches/iter_${ITERATION}_invocations.log")
    GROUP_EXIT=$?

    INVOCATIONS=$(grep -c "test" "patches/iter_${ITERATION}_invocations.log" 2>/dev/null || echo 0)
    cat "patches/iter_${ITERATION}_invocations.log" >&2

    GROUP=()
    if [ $GROUP_EXIT -eq 0 ] && [ -n "$GROUP_OUTPUT" ]; then
        while IFS= read -r line; do
            [[ -n "$line" ]] && GROUP+=("$line")
        done <<< "$GROUP_OUTPUT"
    fi

    ITER_END=$(python3 -c "import time; print(int(time.time()*1000))")
    ITER_DURATION=$(( ITER_END - ITER_START ))

    # Handle failure
    if [ ${#GROUP[@]} -eq 0 ]; then
        log_warning "No buildable group found for the remaining ${REMAINING_HUNK_COUNT} hunk(s)."
        metrics_event "ITER_FAILED" \
            "iteration=${ITERATION},invocations=${INVOCATIONS},duration_ms=${ITER_DURATION},remaining=${REMAINING_HUNK_COUNT}"
        break
    fi

    # Save the group as a merged patch file
    (( GROUP_COUNT++ ))
    GROUP_FILE=$(printf "%s/group_%04d.patch" "$GROUPS_DIR" "$GROUP_COUNT")
    cat "${GROUP[@]}" > "$GROUP_FILE"

    GROUP_NAMES=""
    for g in "${GROUP[@]}"; do
        [[ -n "$GROUP_NAMES" ]] && GROUP_NAMES+=", "
        GROUP_NAMES+=$(basename "$g")
    done
    log_success "Group ${GROUP_COUNT} (${#GROUP[@]} hunk(s)) → $GROUP_FILE  [$GROUP_NAMES]"

    # Commit the group so HEAD advances and re-diff shrinks next iteration
    git add -A
    git commit -m "etc[${GROUP_COUNT}]: ${#GROUP[@]} hunk(s) — $GROUP_NAMES"

    metrics_event "ITER_GROUP" \
        "iteration=${ITERATION},group=${GROUP_COUNT},group_size=${#GROUP[@]},invocations=${INVOCATIONS},duration_ms=${ITER_DURATION},hunks=${GROUP_NAMES}"
done

# ----- Step 7: Results -----
metrics_event "RUN_END" "groups=${GROUP_COUNT}"

log_success "Done: ${GROUP_COUNT} group(s) produced from ${TOTAL_HUNK_COUNT} hunk(s)."
log_info "Full original patch : patches/full.patch"
log_info "Group patches       : patches/groups/group_*.patch"
log_info "Per-iteration diffs : patches/remaining_iter_*.patch"
log_info "Per-iteration probes: patches/iter_*_invocations.log"
log_info "Metrics log         : patches/metrics.log"
log_info "Run:  python3 scripts/metrics.py patches/metrics.log  for a summary."

# ----- Step 8: Cleanup (uncomment when happy with results) -----
#git checkout "$ORIGINAL_BRANCH"
#git branch -d detangling
#log_success "Returned to '$ORIGINAL_BRANCH' and deleted 'detangling' branch."
